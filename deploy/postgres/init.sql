SELECT 'CREATE DATABASE ecommerce'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'ecommerce')\gexec

SELECT 'CREATE DATABASE customer_service'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'customer_service')\gexec

SELECT 'CREATE DATABASE ai_customer_service'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'ai_customer_service')\gexec

\connect ai_customer_service
CREATE EXTENSION IF NOT EXISTS vector;
