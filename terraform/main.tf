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
# EC2 - RIGHT-SIZED FOR DEV
# ---------------------------------------------------------------------------

resource "aws_instance" "app_server" {
  ami                    = var.ec2_ami_id
  instance_type          = var.ec2_instance_type
  subnet_id              = aws_subnet.primary.id
  vpc_security_group_ids = [aws_security_group.app.id]

  root_block_device {
    volume_type = "gp3" # switched to gp3 (cheaper) and reduced size
    volume_size = 30    # reduced from 250GB to 30GB
  }

  tags = {
    Name        = "${var.project_name}-app-server"
    Environment = var.environment
  }
}

resource "aws_instance" "worker" {
  ami                    = var.ec2_ami_id
  instance_type          = var.worker_instance_type
  subnet_id              = aws_subnet.secondary.id
  vpc_security_group_ids = [aws_security_group.app.id]

  root_block_device {
    volume_type = "gp3" # switched to gp3 (cheaper) and reduced size
    volume_size = 30    # reduced from 100GB to 30GB
  }

  tags = {
    Name        = "${var.project_name}-worker"
    Environment = var.environment
  }
}

# ---------------------------------------------------------------------------
# RDS - RIGHT-SIZED FOR DEV
# ---------------------------------------------------------------------------

resource "aws_db_instance" "primary" {
  identifier     = "${var.project_name}-db"
  engine         = "postgres"
  engine_version = "15.4"

  instance_class    = var.rds_instance_class
  allocated_storage = 20    # reduced from 500GB to 20GB
  storage_type      = "gp3" # switched from provisioned IOPS to gp3
  # iops removed – not needed for gp3

  multi_az               = false # removed unnecessary Multi-AZ for dev
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]

  db_name  = "appdb"
  username = var.db_username
  password = var.db_password

  backup_retention_period = 7 # reduced from 30 days
  skip_final_snapshot     = true
  deletion_protection     = false

  tags = {
    Name        = "${var.project_name}-db"
    Environment = var.environment
  }
}
