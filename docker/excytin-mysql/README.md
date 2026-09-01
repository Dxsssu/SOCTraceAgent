# ExCyTIn-Bench MySQL environment

This deployment mirrors the upstream benchmark: eight isolated MySQL 9.0
containers, one for each incident, all using the `env_monitor_db` database.
Ports and container names match `secgym.excytin_env`.

## Why MySQL

The benchmark and its published trajectories use MySQL-specific behavior,
including `SHOW TABLES`, `DESCRIBE`, MySQL error messages, and
`SET SESSION MAX_EXECUTION_TIME`. SQLite can be useful for small unit tests,
but is not an equivalent benchmark backend.

## Prepare data and SQL

From the repository root:

```bash
mkdir -p data/excytin-bench/mysql
tar -xzf data/excytin-bench/huggingface/data_anonymized.tar.gz \
  -C data/excytin-bench/mysql
uv run python docker/excytin-mysql/prepare.py
```

## Start

```bash
docker compose \
  --env-file docker/excytin-mysql/.env \
  --file docker/excytin-mysql/compose.yml \
  up -d
```

The first start imports the CSV data and can take several minutes. Later
starts reuse the named volumes.

| Incident | Host port | Container |
|---|---:|---|
| incident_5 | 3306 | `incident_5` |
| incident_38 | 3307 | `incident_38` |
| incident_34 | 3308 | `incident_34` |
| incident_39 | 3309 | `incident_39` |
| incident_55 | 3310 | `incident_55` |
| incident_134 | 3311 | `incident_134` |
| incident_166 | 3312 | `incident_166` |
| incident_322 | 3313 | `incident_322` |

Connection defaults retained for upstream compatibility:

- database: `env_monitor_db`
- user: `root`
- password: stored in the ignored `.env` file

Every port is bound to `127.0.0.1` and is not exposed to the network.

## Inspect

```bash
docker compose \
  --env-file docker/excytin-mysql/.env \
  --file docker/excytin-mysql/compose.yml \
  ps

set -a
source docker/excytin-mysql/.env
set +a
docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" incident_55 mysql \
  --user=root --database=env_monitor_db \
  --execute="SHOW TABLES;"
```

## Stop

```bash
docker compose \
  --env-file docker/excytin-mysql/.env \
  --file docker/excytin-mysql/compose.yml \
  stop
```

`stop` keeps all imported data. Do not use `down --volumes` unless the local
databases should be deleted and rebuilt from the CSV files.
