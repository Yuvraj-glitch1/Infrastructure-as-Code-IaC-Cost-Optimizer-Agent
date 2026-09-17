#!/usr/bin/env python3
# optimizer.py
"""
IaC Cost Optimizer Agent.

Pipeline:
  1. Run `infracost breakdown/diff` against a Terraform directory, capture JSON.
  2. Parse the JSON into a structured cost report (total monthly cost + top drivers).
  3. Build a strict system prompt containing the raw .tf sources and the cost data.
  4. Call an LLM (Anthropic by default, OpenAI as fallback provider) to produce
     syntax-correct, cheaper Terraform.
  5. Parse the structured response and overwrite the .tf files on disk.

Designed to run non-interactively inside GitHub Actions. Exits 0 when it has
either written optimizations or found nothing worth changing; exits non-zero
only on unrecoverable errors.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Literal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
INFRACOST_TIMEOUT_SECONDS: Final[int] = 600
MAX_LLM_ATTEMPTS: Final[int] = 5
INITIAL_BACKOFF_SECONDS: Final[float] = 2.0
BACKOFF_MULTIPLIER: Final[float] = 2.0
MAX_BACKOFF_SECONDS: Final[float] = 60.0
TOP_COST_DRIVERS: Final[int] = 12
DEFAULT_ANTHROPIC_MODEL: Final[str] = "claude-sonnet-4-5"
DEFAULT_OPENAI_MODEL: Final[str] = "gpt-4o"
BACKUP_SUFFIX: Final[str] = ".pre-optimizer.bak"

Provider = Literal["anthropic", "openai"]

logger: Final[logging.Logger] = logging.getLogger("iac-cost-optimizer")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OptimizerError(RuntimeError):
    """Base class for all agent-level failures."""


class InfracostError(OptimizerError):
    """Raised when the Infracost CLI is missing, fails, or returns unusable output."""


class LLMError(OptimizerError):
    """Raised when the LLM call fails after exhausting retries."""


class ResponseParseError(OptimizerError):
    """Raised when the LLM response cannot be parsed into file edits."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostDriver:
    """A single resource and the monthly cost attributed to it."""

    address: str
    resource_type: str
    monthly_cost: float
    sub_components: dict[str, float] = field(default_factory=dict)

    def to_prompt_line(self) -> str:
        parts: list[str] = [
            f"- {self.address} ({self.resource_type}): ${self.monthly_cost:,.2f}/month"
        ]
        for name, cost in sorted(
            self.sub_components.items(), key=lambda kv: kv[1], reverse=True
        ):
            parts.append(f"    * {name}: ${cost:,.2f}/month")
        return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class CostReport:
    """Parsed, flattened view of an Infracost JSON document."""

    total_monthly_cost: float
    currency: str
    drivers: tuple[CostDriver, ...]
    unsupported_resources: tuple[str, ...]

    def top_drivers(self, limit: int = TOP_COST_DRIVERS) -> tuple[CostDriver, ...]:
        return tuple(
            sorted(self.drivers, key=lambda d: d.monthly_cost, reverse=True)[:limit]
        )

    def to_prompt_block(self) -> str:
        lines: list[str] = [
            f"TOTAL ESTIMATED MONTHLY COST: {self.currency} {self.total_monthly_cost:,.2f}",
            "",
            "COST DRIVERS (highest first):",
        ]
        drivers = self.top_drivers()
        if not drivers:
            lines.append("- (none priced)")
        else:
            lines.extend(d.to_prompt_line() for d in drivers)
        if self.unsupported_resources:
            lines.append("")
            lines.append(
                "RESOURCES NOT PRICED BY INFRACOST (cost unknown, treat conservatively):"
            )
            lines.extend(f"- {addr}" for addr in self.unsupported_resources)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class FileEdit:
    """A single optimized file returned by the LLM."""

    filename: str
    content: str


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    """Full structured response from the LLM."""

    files: tuple[FileEdit, ...]
    rationale: str
    estimated_monthly_savings: float | None


# ---------------------------------------------------------------------------
# Infracost integration
# ---------------------------------------------------------------------------


def ensure_infracost_available() -> str:
    """Return the resolved path to the infracost binary, or raise."""
    binary: str | None = shutil.which("infracost")
    if binary is None:
        raise InfracostError(
            "The `infracost` CLI was not found on PATH. Install it from "
            "https://www.infracost.io/docs/#quick-start and authenticate with "
            "`infracost auth login` or the INFRACOST_API_KEY environment variable."
        )
    logger.debug("Resolved infracost binary at %s", binary)
    return binary


def run_infracost(
    terraform_dir: Path,
    *,
    use_diff: bool = True,
    timeout: int = INFRACOST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """
    Execute Infracost against `terraform_dir` and return the parsed JSON document.

    Tries `infracost diff` first (so the agent sees the delta introduced by the PR).
    When no baseline snapshot exists — the usual case on a first run — Infracost
    errors out, and we transparently fall back to `infracost breakdown`.
    """
    binary: str = ensure_infracost_available()

    if not terraform_dir.is_dir():
        raise InfracostError(f"Terraform directory does not exist: {terraform_dir}")

    subcommands: list[str] = ["diff", "breakdown"] if use_diff else ["breakdown"]
    last_error: str = ""

    for subcommand in subcommands:
        cmd: list[str] = [
            binary,
            subcommand,
            "--path",
            str(terraform_dir),
            "--format",
            "json",
        ]
        if subcommand == "diff":
            # Compare against an empty baseline so `diff` succeeds standalone.
            cmd.extend(["--compare-to", "/dev/null"])

        logger.info("Running: %s", " ".join(cmd))
        try:
            completed: subprocess.CompletedProcess[str] = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as exc:
            raise InfracostError(
                f"Infracost timed out after {timeout}s running `{subcommand}`."
            ) from exc
        except OSError as exc:
            raise InfracostError(f"Failed to execute infracost: {exc}") from exc

        if completed.returncode != 0 or not completed.stdout.strip():
            last_error = (completed.stderr or completed.stdout or "").strip()
            logger.warning(
                "infracost %s exited with code %d; %s",
                subcommand,
                completed.returncode,
                "falling back" if subcommand != subcommands[-1] else "no fallback left",
            )
            logger.debug("infracost stderr: %s", last_error)
            continue

        try:
            document: dict[str, Any] = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            last_error = f"Invalid JSON from infracost {subcommand}: {exc}"
            logger.warning(last_error)
            continue

        logger.info("Infracost `%s` completed successfully.", subcommand)
        return document

    raise InfracostError(
        f"All Infracost invocations failed. Last error: {last_error or 'unknown'}"
    )


def _coerce_float(value: Any) -> float:
    """Infracost emits costs as JSON strings; coerce defensively."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _walk_resources(resources: Iterable[dict[str, Any]]) -> list[CostDriver]:
    """Flatten Infracost resources (including nested subresources) into CostDrivers."""
    drivers: list[CostDriver] = []

    for resource in resources:
        if not isinstance(resource, dict):
            continue

        address: str = str(resource.get("address") or resource.get("name") or "unknown")
        resource_type: str = str(resource.get("resourceType") or "unknown")
        monthly_cost: float = _coerce_float(resource.get("monthlyCost"))

        sub_components: dict[str, float] = {}
        for component in resource.get("costComponents") or []:
            if not isinstance(component, dict):
                continue
            component_name: str = str(component.get("name") or "component")
            sub_components[component_name] = _coerce_float(component.get("monthlyCost"))

        drivers.append(
            CostDriver(
                address=address,
                resource_type=resource_type,
                monthly_cost=monthly_cost,
                sub_components=sub_components,
            )
        )

        nested: list[dict[str, Any]] = resource.get("subresources") or []
        if nested:
            for sub in _walk_resources(nested):
                drivers.append(
                    CostDriver(
                        address=f"{address} -> {sub.address}",
                        resource_type=sub.resource_type,
                        monthly_cost=sub.monthly_cost,
                        sub_components=sub.sub_components,
                    )
                )

    return drivers


def parse_cost_report(document: dict[str, Any]) -> CostReport:
    """Turn a raw Infracost JSON document into a CostReport."""
    if not isinstance(document, dict):
        raise InfracostError("Infracost output was not a JSON object.")

    projects: list[dict[str, Any]] = document.get("projects") or []
    if not projects:
        logger.warning("Infracost returned no projects; cost report will be empty.")

    all_drivers: list[CostDriver] = []
    unsupported: list[str] = []

    for project in projects:
        if not isinstance(project, dict):
            continue
        breakdown: dict[str, Any] = (
            project.get("breakdown") or project.get("diff") or {}
        )
        resources: list[dict[str, Any]] = breakdown.get("resources") or []
        all_drivers.extend(_walk_resources(resources))

        for resource in resources:
            if isinstance(resource, dict) and resource.get("monthlyCost") is None:
                unsupported.append(str(resource.get("address") or "unknown"))

    total: float = _coerce_float(
        document.get("totalMonthlyCost")
        or document.get("diffTotalMonthlyCost")
    )
    if total == 0.0 and all_drivers:
        total = sum(d.monthly_cost for d in all_drivers if "->" not in d.address)

    currency: str = str(document.get("currency") or "USD")

    report = CostReport(
        total_monthly_cost=total,
        currency=currency,
        drivers=tuple(all_drivers),
        unsupported_resources=tuple(dict.fromkeys(unsupported)),
    )
    logger.info(
        "Parsed cost report: %s %.2f/month across %d priced resources.",
        report.currency,
        report.total_monthly_cost,
        len(report.drivers),
    )
    return report


# ---------------------------------------------------------------------------
# Terraform file IO
# ---------------------------------------------------------------------------


def read_terraform_files(terraform_dir: Path) -> dict[str, str]:
    """Read every *.tf file in the directory (non-recursive) into {filename: content}."""
    files: dict[str, str] = {}
    for path in sorted(terraform_dir.glob("*.tf")):
        try:
            files[path.name] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            logger.error("Skipping %s: not valid UTF-8 (%s)", path.name, exc)
    if not files:
        raise OptimizerError(f"No .tf files found in {terraform_dir}")
    logger.info("Loaded %d Terraform file(s): %s", len(files), ", ".join(files))
    return files


def write_terraform_files(
    terraform_dir: Path,
    edits: Iterable[FileEdit],
    *,
    make_backups: bool = True,
) -> list[str]:
    """
    Overwrite .tf files on disk with optimized content.

    Returns the list of filenames that actually changed. Files whose content is
    byte-identical to what is already on disk are skipped so the git diff stays clean.
    """
    changed: list[str] = []

    for edit in edits:
        # Guard against path traversal from model output.
        safe_name: str = Path(edit.filename).name
        if not safe_name.endswith(".tf"):
            logger.warning("Refusing to write non-.tf file: %s", edit.filename)
            continue

        target: Path = terraform_dir / safe_name
        new_content: str = edit.content.rstrip() + "\n"

        if target.exists():
            existing: str = target.read_text(encoding="utf-8")
            if existing == new_content:
                logger.info("No change for %s; skipping write.", safe_name)
                continue
            if make_backups:
                backup: Path = target.with_suffix(target.suffix + BACKUP_SUFFIX)
                backup.write_text(existing, encoding="utf-8")
                logger.debug("Backed up %s -> %s", safe_name, backup.name)

        target.write_text(new_content, encoding="utf-8")
        changed.append(safe_name)
        logger.info("Wrote optimized %s (%d bytes).", safe_name, len(new_content))

    return changed


def cleanup_backups(terraform_dir: Path) -> None:
    """Remove the .bak files so they never end up in the commit."""
    for backup in terraform_dir.glob(f"*{BACKUP_SUFFIX}"):
        backup.unlink(missing_ok=True)
        logger.debug("Removed backup %s", backup.name)


def validate_terraform(terraform_dir: Path) -> bool:
    """
    Best-effort syntax gate: run `terraform fmt -check` style validation.

    Uses `terraform fmt` (which parses HCL) rather than `terraform validate`,
    because validate requires `terraform init` and provider downloads.
    Returns True when the files parse, False otherwise.
    """
    binary: str | None = shutil.which("terraform")
    if binary is None:
        logger.warning("terraform binary not found; skipping syntax validation.")
        return True

    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [binary, "fmt", "-write=true", "-list=true", str(terraform_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        logger.error(
            "terraform fmt reported a parse error:\n%s",
            (completed.stderr or completed.stdout).strip(),
        )
        return False
    logger.info("Terraform syntax validation passed.")
    return True


def restore_backups(terraform_dir: Path) -> None:
    """Roll every file back to its pre-optimization content."""
    for backup in terraform_dir.glob(f"*{BACKUP_SUFFIX}"):
        original: Path = backup.with_suffix("")  # strips BACKUP_SUFFIX
        original = terraform_dir / original.name.replace(BACKUP_SUFFIX, "")
        original.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        backup.unlink(missing_ok=True)
        logger.warning("Rolled back %s from backup.", original.name)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT: Final[str] = """\
You are a Principal Cloud Cost Engineer. You right-size Terraform (AWS provider) \
infrastructure to reduce monthly spend while preserving the architecture's intent.

RULES — follow all of them exactly:
1. Preserve every resource block, resource address, variable name, and output name \
unless removing a resource is unambiguously safe for the stated environment. Never \
rename a resource (that forces a destroy/create and breaks downstream references).
2. Right-size, do not re-architect. Change instance types, storage classes, volume \
sizes, IOPS, retention windows, and redundancy flags. Do not swap EC2 for Lambda, \
introduce new modules, or add resources that did not exist.
3. Environment drives aggressiveness:
   - dev/test: optimize hard. Burstable instances (t3/t4g family), gp3 storage, \
single-AZ, minimal backup retention, drop read replicas.
   - staging: moderate. Keep redundancy only where it is load-bearing.
   - prod: conservative. Never remove Multi-AZ, replicas, or backups. Only make \
changes that carry no availability or durability risk (e.g. gp2 -> gp3, \
previous-gen -> current-gen instance family at equal or better specs).
4. Prefer changing the `default` value of a variable in variables.tf over hardcoding \
a value in main.tf when the resource already references a variable.
5. Output must be valid HCL that parses with `terraform fmt`. Keep existing comments \
where they still apply, and add a short comment on each line you change explaining \
the saving.
6. Do not touch credentials, AMI IDs, CIDR blocks, regions, or security group rules.
7. If a file requires no change, omit it from your output entirely.

OUTPUT FORMAT — return a single JSON object and nothing else. No prose before or \
after, no markdown code fences around the JSON:
{
  "estimated_monthly_savings": <number, USD>,
  "rationale": "<2-6 sentences explaining the changes and why they are safe>",
  "files": [
    {"filename": "main.tf", "content": "<full contents of the optimized file>"}
  ]
}
The "content" field must contain the COMPLETE file, not a diff or a fragment.\
"""


def build_user_prompt(
    terraform_files: dict[str, str],
    report: CostReport,
    environment_hint: str,
) -> str:
    """Assemble the user-turn payload: cost data + raw Terraform sources."""
    sections: list[str] = [
        "## INFRACOST ANALYSIS",
        "",
        report.to_prompt_block(),
        "",
        f"## DECLARED ENVIRONMENT: {environment_hint}",
        "",
        "## CURRENT TERRAFORM SOURCE",
        "",
    ]

    for filename, content in terraform_files.items():
        sections.append(f"### FILE: {filename}")
        sections.append("```hcl")
        sections.append(content.rstrip())
        sections.append("```")
        sections.append("")

    sections.append(
        "Right-size this infrastructure according to your rules and return the "
        "JSON object described in your instructions."
    )
    return "\n".join(sections)


def detect_environment(terraform_files: dict[str, str]) -> str:
    """Infer the environment from the `environment` variable default, if present."""
    pattern: re.Pattern[str] = re.compile(
        r'variable\s+"environment"\s*\{[^}]*?default\s*=\s*"([^"]+)"',
        re.DOTALL,
    )
    for content in terraform_files.values():
        match: re.Match[str] | None = pattern.search(content)
        if match:
            env: str = match.group(1)
            logger.info("Detected environment from variables: %s", env)
            return env
    logger.info("Could not detect environment; defaulting to 'dev'.")
    return "dev"


# ---------------------------------------------------------------------------
# LLM invocation
# ---------------------------------------------------------------------------


def _is_retryable(exc: BaseException) -> bool:
    """Heuristic: retry rate limits, overloads, and transient 5xx/network errors."""
    name: str = type(exc).__name__.lower()
    message: str = str(exc).lower()
    retryable_tokens: tuple[str, ...] = (
        "ratelimit",
        "rate_limit",
        "rate limit",
        "429",
        "overloaded",
        "529",
        "500",
        "502",
        "503",
        "504",
        "timeout",
        "timed out",
        "connection",
        "apistatus",
        "internalserver",
    )
    return any(token in name or token in message for token in retryable_tokens)


def call_anthropic(system_prompt: str, user_prompt: str, model: str) -> str:
    """Call the Anthropic Messages API with exponential backoff."""
    try:
        from anthropic import Anthropic
    except ImportError as exc:  # pragma: no cover
        raise LLMError(
            "The `anthropic` package is not installed. Run: pip install anthropic"
        ) from exc

    api_key: str | None = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY is not set in the environment.")

    client = Anthropic(api_key=api_key)
    backoff: float = INITIAL_BACKOFF_SECONDS
    last_exc: BaseException | None = None

    for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
        try:
            logger.info("Anthropic call, attempt %d/%d", attempt, MAX_LLM_ATTEMPTS)
            response = client.messages.create(
                model=model,
                max_tokens=8192,
                temperature=0.0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text: str = "".join(
                block.text for block in response.content if block.type == "text"
            )
            if not text.strip():
                raise LLMError("Anthropic returned an empty response body.")
            return text
        except Exception as exc:  # noqa: BLE001 - SDK exception surface varies
            last_exc = exc
            if attempt == MAX_LLM_ATTEMPTS or not _is_retryable(exc):
                break
            logger.warning(
                "Retryable error (%s). Sleeping %.1fs before retry.",
                type(exc).__name__,
                backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SECONDS)

    raise LLMError(f"Anthropic request failed after retries: {last_exc}") from last_exc


def call_openai(system_prompt: str, user_prompt: str, model: str) -> str:
    """Call the OpenAI Chat Completions API with exponential backoff."""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise LLMError(
            "The `openai` package is not installed. Run: pip install openai"
        ) from exc

    api_key: str | None = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMError("OPENAI_API_KEY is not set in the environment.")

    client = OpenAI(api_key=api_key)
    backoff: float = INITIAL_BACKOFF_SECONDS
    last_exc: BaseException | None = None

    for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
        try:
            logger.info("OpenAI call, attempt %d/%d", attempt, MAX_LLM_ATTEMPTS)
            response = client.chat.completions.create(
                model=model,
                temperature=0.0,
                max_tokens=8192,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            text: str | None = response.choices[0].message.content
            if not text or not text.strip():
                raise LLMError("OpenAI returned an empty response body.")
            return text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == MAX_LLM_ATTEMPTS or not _is_retryable(exc):
                break
            logger.warning(
                "Retryable error (%s). Sleeping %.1fs before retry.",
                type(exc).__name__,
                backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SECONDS)

    raise LLMError(f"OpenAI request failed after retries: {last_exc}") from last_exc


def invoke_llm(
    provider: Provider, system_prompt: str, user_prompt: str, model: str | None
) -> str:
    if provider == "anthropic":
        return call_anthropic(system_prompt, user_prompt, model or DEFAULT_ANTHROPIC_MODEL)
    if provider == "openai":
        return call_openai(system_prompt, user_prompt, model or DEFAULT_OPENAI_MODEL)
    raise LLMError(f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _extract_json_object(raw: str) -> str:
    """
    Pull a JSON object out of a model response that may be wrapped in prose or fences.

    Strategy: strip markdown fences, then brace-match from the first `{` while
    respecting string literals and escapes, so embedded HCL containing braces
    does not break the scan.
    """
    text: str = raw.strip()

    fence: re.Match[str] | None = re.search(
        r"```(?:json)?\s*(.*?)```", text, re.DOTALL
    )
    if fence:
        text = fence.group(1).strip()

    start: int = text.find("{")
    if start == -1:
        raise ResponseParseError("No JSON object found in the LLM response.")

    depth: int = 0
    in_string: bool = False
    escaped: bool = False

    for index in range(start, len(text)):
        char: str = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    raise ResponseParseError("Unterminated JSON object in the LLM response.")


def parse_llm_response(raw: str) -> OptimizationResult:
    """Parse and validate the model's JSON payload into an OptimizationResult."""
    candidate: str = _extract_json_object(raw)

    try:
        payload: Any = json.loads(candidate)
    except json.JSONDecodeError as exc:
        # Second chance: models sometimes emit literal newlines inside JSON strings.
        repaired: str = re.sub(r"(?<!\\)\n(?=(?:[^\"]*\"[^\"]*\")*[^\"]*\"[^\"]*$)", r"\\n", candidate)
        try:
            payload = json.loads(repaired)
        except json.JSONDecodeError:
            raise ResponseParseError(
                f"LLM response was not valid JSON: {exc}. "
                f"First 500 chars: {candidate[:500]!r}"
            ) from exc

    if not isinstance(payload, dict):
        raise ResponseParseError("LLM JSON payload was not an object.")

    raw_files: Any = payload.get("files")
    if not isinstance(raw_files, list):
        raise ResponseParseError("LLM payload is missing a `files` array.")

    edits: list[FileEdit] = []
    for entry in raw_files:
        if not isinstance(entry, dict):
            logger.warning("Skipping non-object entry in `files`.")
            continue
        filename: Any = entry.get("filename")
        content: Any = entry.get("content")
        if not isinstance(filename, str) or not isinstance(content, str):
            logger.warning("Skipping malformed file entry: %r", entry)
            continue
        if not content.strip():
            logger.warning("Skipping empty content for %s.", filename)
            continue
        edits.append(FileEdit(filename=filename, content=content))

    if not edits:
        raise ResponseParseError("LLM returned no usable file edits.")

    savings_raw: Any = payload.get("estimated_monthly_savings")
    savings: float | None
    try:
        savings = float(savings_raw) if savings_raw is not None else None
    except (TypeError, ValueError):
        savings = None

    return OptimizationResult(
        files=tuple(edits),
        rationale=str(payload.get("rationale") or "No rationale supplied."),
        estimated_monthly_savings=savings,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def write_step_summary(
    report: CostReport, result: OptimizationResult, changed: list[str]
) -> None:
    """Append a markdown summary to the GitHub Actions job summary, if available."""
    summary_path: str | None = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines: list[str] = [
        "## AI Cost Optimizer Report",
        "",
        f"**Baseline monthly cost:** {report.currency} {report.total_monthly_cost:,.2f}",
    ]
    if result.estimated_monthly_savings is not None:
        lines.append(
            f"**Estimated monthly savings:** {report.currency} "
            f"{result.estimated_monthly_savings:,.2f}"
        )
    lines.extend(
        [
            "",
            "### Rationale",
            "",
            result.rationale,
            "",
            "### Files rewritten",
            "",
        ]
    )
    lines.extend(f"- `{name}`" for name in changed) if changed else lines.append(
        "- _(no changes required)_"
    )
    lines.extend(["", "### Top cost drivers (before)", ""])
    lines.extend(
        f"- `{d.address}` — {report.currency} {d.monthly_cost:,.2f}/mo"
        for d in report.top_drivers(8)
    )

    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        logger.warning("Could not write job summary: %s", exc)


def set_output(name: str, value: str) -> None:
    """Emit a GitHub Actions step output."""
    output_path: str | None = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    try:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    except OSError as exc:
        logger.warning("Could not write step output %s: %s", name, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze Terraform cost with Infracost and rewrite it via an LLM."
    )
    parser.add_argument(
        "--terraform-dir",
        type=Path,
        default=Path(os.environ.get("TERRAFORM_DIR", "terraform")),
        help="Directory containing the .tf files to optimize.",
    )
    parser.add_argument(
        "--provider",
        choices=("anthropic", "openai"),
        default=os.environ.get("LLM_PROVIDER", "anthropic"),
        help="LLM provider to use.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=os.environ.get("LLM_MODEL") or None,
        help="Model identifier. Defaults per provider.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and print the proposed changes without writing files.",
    )
    parser.add_argument(
        "--no-diff",
        action="store_true",
        help="Skip `infracost diff` and go straight to `infracost breakdown`.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
        stream=sys.stdout,
    )

    terraform_dir: Path = args.terraform_dir.resolve()
    logger.info("Optimizing Terraform in %s", terraform_dir)

    try:
        terraform_files: dict[str, str] = read_terraform_files(terraform_dir)
        document: dict[str, Any] = run_infracost(
            terraform_dir, use_diff=not args.no_diff
        )
        report: CostReport = parse_cost_report(document)
    except (InfracostError, OptimizerError) as exc:
        logger.error("Cost analysis failed: %s", exc)
        set_output("changes_made", "false")
        return 1

    if report.total_monthly_cost <= 0.0:
        logger.info("No priced resources found; nothing to optimize.")
        set_output("changes_made", "false")
        return 0

    environment: str = detect_environment(terraform_files)
    user_prompt: str = build_user_prompt(terraform_files, report, environment)
    logger.debug("Prompt size: %d characters", len(user_prompt))

    try:
        raw_response: str = invoke_llm(
            args.provider, SYSTEM_PROMPT, user_prompt, args.model
        )
        result: OptimizationResult = parse_llm_response(raw_response)
    except (LLMError, ResponseParseError) as exc:
        logger.error("Optimization failed: %s", exc)
        set_output("changes_made", "false")
        return 1

    logger.info("LLM rationale: %s", result.rationale)
    if result.estimated_monthly_savings is not None:
        logger.info(
            "Estimated monthly savings: %s %.2f",
            report.currency,
            result.estimated_monthly_savings,
        )

    if args.dry_run:
        for edit in result.files:
            print(f"\n===== PROPOSED {edit.filename} =====\n{edit.content}")
        set_output("changes_made", "false")
        return 0

    changed: list[str] = write_terraform_files(terraform_dir, result.files)

    if changed and not validate_terraform(terraform_dir):
        logger.error("Optimized Terraform failed syntax validation; rolling back.")
        restore_backups(terraform_dir)
        set_output("changes_made", "false")
        return 1

    cleanup_backups(terraform_dir)

    if not changed:
        logger.info("Infrastructure is already optimal; no files written.")
        set_output("changes_made", "false")
        return 0

    logger.info("Rewrote %d file(s): %s", len(changed), ", ".join(changed))
    set_output("changes_made", "true")
    set_output("changed_files", " ".join(changed))
    if result.estimated_monthly_savings is not None:
        set_output("savings", f"{result.estimated_monthly_savings:.2f}")
    write_step_summary(report, result, changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
