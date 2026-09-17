# terraform/variables.tf
# Input variables for the baseline (deliberately over-provisioned) stack.

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Name prefix applied to all resources."
  type        = string
  default     = "acme-dev"
}

variable "environment" {
  description = "Deployment environment. Drives right-sizing decisions made by the optimizer agent."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "ec2_ami_id" {
  description = "AMI ID for EC2 instances (Amazon Linux 2023, us-east-1)."
  type        = string
  default     = "ami-0c101f26f147fa7fd"
}

variable "ec2_instance_type" {
  description = "Instance type for the primary application server."
  type        = string
  default     = "t2.2xlarge" # 8 vCPU / 32 GiB - grossly oversized for a dev web app
}

variable "worker_instance_type" {
  description = "Instance type for the background worker node."
  type        = string
  default     = "m5.4xlarge" # 16 vCPU / 64 GiB - grossly oversized for a dev worker
}

variable "rds_instance_class" {
  description = "Instance class for the RDS Postgres instances."
  type        = string
  default     = "db.m5.2xlarge" # oversized for a dev database
}

variable "db_username" {
  description = "Master username for the RDS instance."
  type        = string
  default     = "appadmin"
}

variable "db_password" {
  description = "Master password for the RDS instance. Supply via TF_VAR_db_password, never commit."
  type        = string
  sensitive   = true
  default     = "ChangeMe_NotASecret123!"
}
