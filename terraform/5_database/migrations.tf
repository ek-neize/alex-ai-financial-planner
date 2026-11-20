# Automatic schema migrations and seed data
#
# After the cluster is up, run backend/database/run_migrations.py and
# seed_data.py on the machine running Terraform (via uv). A freshly created
# or recreated database therefore always has its tables and reference data,
# so there's no manual step to forget.
#
# - Migrations re-run whenever a migrations/*.sql file changes. The runner
#   tracks applied migrations, so only new ones are applied.
# - Seed data loads once per cluster. It's not re-run later, because it would
#   reset instrument prices that the agents keep up to date.
#
# Set run_database_setup = false to skip this and run the scripts by hand.

locals {
  database_dir = "${path.module}/../../backend/database"

  database_env = {
    AURORA_CLUSTER_ARN = aws_rds_cluster.aurora.arn
    AURORA_SECRET_ARN  = aws_secretsmanager_secret.db_credentials.arn
    AURORA_DATABASE    = aws_rds_cluster.aurora.database_name
    DEFAULT_AWS_REGION = var.aws_region
  }

  migrations_hash = sha256(join("", [
    for f in sort(fileset("${local.database_dir}/migrations", "*.sql")) :
    filesha256("${local.database_dir}/migrations/${f}")
  ]))
}

resource "terraform_data" "migrations" {
  count = var.run_database_setup ? 1 : 0

  triggers_replace = [
    aws_rds_cluster.aurora.cluster_resource_id, # new cluster
    local.migrations_hash,                      # new or changed migration files
  ]

  provisioner "local-exec" {
    working_dir = local.database_dir
    command     = "uv run run_migrations.py"
    environment = local.database_env
  }

  depends_on = [
    aws_rds_cluster_instance.aurora,
    aws_secretsmanager_secret_version.db_credentials,
  ]
}

resource "terraform_data" "seed" {
  count = var.run_database_setup ? 1 : 0

  triggers_replace = [aws_rds_cluster.aurora.cluster_resource_id]

  provisioner "local-exec" {
    working_dir = local.database_dir
    command     = "uv run seed_data.py"
    environment = local.database_env
  }

  depends_on = [terraform_data.migrations]
}
