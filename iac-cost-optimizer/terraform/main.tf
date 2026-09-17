# terraform/main.tf
# Intentionally over-provisioned baseline infrastructure.
# This represents the "bad" starting state that the AI Cost Optimizer Agent
# is expected to detect and rewrite into a cheaper, right-sized equivalent.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ---------------------------------------------------------------------------
# Networking (minimal, just enough for the EC2/RDS resources below to plan)
# ---------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name        = "${var.project_name}-vpc"
    Environment = var.environment
  }
}

resource "aws_subnet" "primary" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "${var.aws_region}a"
  map_public_ip_on_launch = true

  tags = {
    Name        = "${var.project_name}-subnet-primary"
    Environment = var.environment
  }
}

resource "aws_subnet" "secondary" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.2.0/24"
  availability_zone       = "${var.aws_region}b"
  map_public_ip_on_launch = true

  tags = {
    Name        = "${var.project_name}-subnet-secondary"
    Environment = var.environment
  }
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.project_name}-db-subnet-group"
  subnet_ids = [aws_subnet.primary.id, aws_subnet.secondary.id]

  tags = {
    Name        = "${var.project_name}-db-subnet-group"
    Environment = var.environment
  }
}

resource "aws_security_group" "app" {
  name        = "${var.project_name}-app-sg"
  description = "Security group for application EC2 instance"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "${var.project_name}-app-sg"
    Environment = var.environment
  }
}

resource "aws_security_group" "db" {
  name        = "${var.project_name}-db-sg"
  description = "Security group for RDS instance"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Postgres from app tier"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "${var.project_name}-db-sg"
    Environment = var.environment
  }
}

# ---------------------------------------------------------------------------
# EC2 - INTENTIONALLY OVER-PROVISIONED
# A t2.2xlarge (8 vCPU / 32GB RAM) running a "dev" environment web app.
# This is the primary cost driver the optimizer agent should flag.
# ---------------------------------------------------------------------------

resource "aws_instance" "app_server" {
  ami                    = var.ec2_ami_id
  instance_type          = var.ec2_instance_type # defaults to t2.2xlarge - WAY oversized for dev
  subnet_id              = aws_subnet.primary.id
  vpc_security_group_ids = [aws_security_group.app.id]

  root_block_device {
    volume_type = "gp2" # legacy, more expensive than gp3 for equivalent IOPS
    volume_size = 200   # oversized for a dev app server
  }

  tags = {
    Name        = "${var.project_name}-app-server"
    Environment = var.environment
  }
}

# A second, always-on large instance for "background workers" that in a dev
# environment realistically sits idle most of the time.
resource "aws_instance" "worker" {
  ami                    = var.ec2_ami_id
  instance_type          = var.worker_instance_type # defaults to m5.4xlarge
  subnet_id              = aws_subnet.secondary.id
  vpc_security_group_ids = [aws_security_group.app.id]

  root_block_device {
    volume_type = "gp2"
    volume_size = 100
  }

  tags = {
    Name        = "${var.project_name}-worker"
    Environment = var.environment
  }
}

# ---------------------------------------------------------------------------
# RDS - INTENTIONALLY OVER-PROVISIONED
# Multi-AZ, large instance class, high provisioned IOPS storage for a
# workload that (per environment tag) is "dev".
# ---------------------------------------------------------------------------

resource "aws_db_instance" "primary" {
  identifier     = "${var.project_name}-db"
  engine         = "postgres"
  engine_version = "15.4"

  instance_class    = var.rds_instance_class # defaults to db.m5.2xlarge - oversized for dev
  allocated_storage = 500                    # GB, oversized
  storage_type      = "io1"                  # provisioned IOPS - expensive, unnecessary for dev
  iops              = 5000

  multi_az            = true # unnecessary redundancy cost for a dev environment
  db_subnet_group_name = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]

  db_name  = "appdb"
  username = var.db_username
  password = var.db_password

  backup_retention_period = 30 # excessive for dev
  skip_final_snapshot     = true
  deletion_protection     = false

  tags = {
    Name        = "${var.project_name}-db"
    Environment = var.environment
  }
}

# A read replica that is almost certainly unnecessary in a dev environment.
resource "aws_db_instance" "replica" {
  identifier          = "${var.project_name}-db-replica"
  replicate_source_db = aws_db_instance.primary.identifier
  instance_class      = var.rds_instance_class
  storage_type         = "io1"
  iops                  = 5000
  publicly_accessible  = false
  skip_final_snapshot  = true

  tags = {
    Name        = "${var.project_name}-db-replica"
    Environment = var.environment
  }
}
