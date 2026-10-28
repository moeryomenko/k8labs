/// PostgreSQL and Redis access with explicit pool-wait measurement.
///
/// Pool wait is separated from operation time on purpose. If the service is
/// throttled, both grow together. If a dependency or its pool is saturated,
/// only dependency time and pool wait grow. That difference is what lets the
/// analysis reject dependency-bound trials instead of blaming the CPU limit.
use std::time::Instant;

use redis::AsyncCommands as _;

use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use deadpool_redis::{Config as RedisConfig, Pool as RedisPool, Runtime};
use tokio_postgres::NoTls;

use crate::telemetry;

pub struct Dependencies {
    postgres: Pool,
    redis: RedisPool,
}

impl Dependencies {
    pub async fn connect(
        postgres_url: &str,
        redis_url: &str,
        postgres_pool_size: usize,
        redis_pool_size: usize,
    ) -> Result<Self, String> {
        let pg_config = postgres_url
            .parse::<tokio_postgres::Config>()
            .map_err(|err| format!("invalid POSTGRES_URL: {err}"))?;
        let manager = Manager::from_config(
            pg_config,
            NoTls,
            ManagerConfig {
                recycling_method: RecyclingMethod::Fast,
            },
        );
        let postgres = Pool::builder(manager)
            .max_size(postgres_pool_size)
            .build()
            .map_err(|err| format!("building PostgreSQL pool: {err}"))?;

        let mut redis_config = RedisConfig::from_url(redis_url);
        redis_config.pool = Some(deadpool_redis::PoolConfig::new(redis_pool_size));
        let redis = redis_config
            .create_pool(Some(Runtime::Tokio1))
            .map_err(|err| format!("building Redis pool: {err}"))?;
        let mut connection = redis
            .get()
            .await
            .map_err(|err| format!("connecting to Redis: {err}"))?;
        let pong: String = redis::cmd("PING")
            .query_async(&mut connection)
            .await
            .map_err(|err| format!("Redis PING failed: {err}"))?;
        if pong != "PONG" {
            return Err(format!("Redis PING returned {pong:?}"));
        }

        Ok(Self { postgres, redis })
    }

    /// Create the schema the workload reads and writes. Idempotent so every
    /// replica can race it during startup.
    pub async fn migrate(&self) -> Result<(), String> {
        let client = self
            .postgres
            .get()
            .await
            .map_err(|err| format!("acquiring PostgreSQL connection: {err}"))?;
        client
            .batch_execute(
                "CREATE TABLE IF NOT EXISTS items (
                     id BIGINT PRIMARY KEY,
                     payload TEXT NOT NULL
                 );",
            )
            .await
            .map_err(|err| format!("creating schema: {err}"))?;
        Ok(())
    }

    pub async fn select_item(&self, id: i64) -> Result<String, String> {
        let wait_start = Instant::now();
        let client = self
            .postgres
            .get()
            .await
            .map_err(|err| format!("PostgreSQL pool: {err}"))?;
        let pool_wait_ms = wait_start.elapsed().as_secs_f64() * 1000.0;

        let start = Instant::now();
        let row = client
            .query_opt("SELECT payload FROM items WHERE id = $1", &[&id])
            .await
            .map_err(|err| format!("SELECT failed: {err}"))?;
        telemetry::record_postgres(start.elapsed().as_secs_f64() * 1000.0, pool_wait_ms, "select");

        Ok(row
            .map(|row| row.get::<_, String>(0))
            .unwrap_or_else(|| "missing".to_string()))
    }

    pub async fn upsert_item(&self, id: i64, payload: &str) -> Result<(), String> {
        let wait_start = Instant::now();
        let client = self
            .postgres
            .get()
            .await
            .map_err(|err| format!("PostgreSQL pool: {err}"))?;
        let pool_wait_ms = wait_start.elapsed().as_secs_f64() * 1000.0;

        let start = Instant::now();
        client
            .execute(
                "INSERT INTO items (id, payload) VALUES ($1, $2)
                 ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload",
                &[&id, &payload],
            )
            .await
            .map_err(|err| format!("UPSERT failed: {err}"))?;
        telemetry::record_postgres(start.elapsed().as_secs_f64() * 1000.0, pool_wait_ms, "upsert");

        Ok(())
    }

    pub async fn cache_get(&self, key: &str) -> Result<Option<String>, String> {
        let wait_start = Instant::now();
        let mut connection = self
            .redis
            .get()
            .await
            .map_err(|err| format!("Redis pool: {err}"))?;
        let pool_wait_ms = wait_start.elapsed().as_secs_f64() * 1000.0;

        let start = Instant::now();
        let value: Option<String> = connection
            .get(key)
            .await
            .map_err(|err| format!("GET failed: {err}"))?;
        telemetry::record_redis(start.elapsed().as_secs_f64() * 1000.0, pool_wait_ms, "get");

        Ok(value)
    }

    pub async fn cache_set(&self, key: &str, value: &str) -> Result<(), String> {
        let wait_start = Instant::now();
        let mut connection = self
            .redis
            .get()
            .await
            .map_err(|err| format!("Redis pool: {err}"))?;
        let pool_wait_ms = wait_start.elapsed().as_secs_f64() * 1000.0;

        let start = Instant::now();
        let _: () = connection
            .set_ex(key, value, 60u64)
            .await
            .map_err(|err| format!("SET failed: {err}"))?;
        telemetry::record_redis(start.elapsed().as_secs_f64() * 1000.0, pool_wait_ms, "set");

        Ok(())
    }

    pub async fn redis_ping(&self) -> Result<(), String> {
        let mut connection = self
            .redis
            .get()
            .await
            .map_err(|err| format!("Redis pool: {err}"))?;
        let pong: String = redis::cmd("PING")
            .query_async(&mut connection)
            .await
            .map_err(|err| format!("Redis PING failed: {err}"))?;
        if pong != "PONG" {
            return Err(format!("Redis PING returned {pong:?}"));
        }
        Ok(())
    }

    pub async fn postgres_ping(&self) -> Result<(), String> {
        let client = self
            .postgres
            .get()
            .await
            .map_err(|err| format!("PostgreSQL pool: {err}"))?;
        client
            .simple_query("SELECT 1")
            .await
            .map_err(|err| format!("PostgreSQL SELECT 1 failed: {err}"))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn pool_config_rejects_zero_signatures_are_checked_elsewhere() {
        // Pool sizing itself is validated in config; here we only assert the
        // manager accepts a well-formed DSN so startup fails with a clear error
        // on a typo rather than a panic.
        let parsed = "postgres://bench:bench@postgres:5432/bench"
            .parse::<tokio_postgres::Config>();
        assert!(parsed.is_ok());
    }
}
