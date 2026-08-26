-- ===========================================================================
-- ORCA Web Lab Cloudflare Control Plane - Encrypted Credential Vault Migration
-- Migration: 0002_credential_vault.sql
-- ===========================================================================

PRAGMA foreign_keys = ON;

-- 1. Encrypted Credential Vault
-- Stores ONLY ciphertext, random nonces, and authentication tags.
-- The Cloudflare Worker NEVER possesses the master key and NEVER decrypts.
CREATE TABLE IF NOT EXISTS credential_vault (
    owner TEXT PRIMARY KEY,
    kaggle_username TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    nonce TEXT NOT NULL,
    tag TEXT NOT NULL,
    encryption_version INTEGER DEFAULT 1,
    status TEXT DEFAULT 'ACTIVE',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_verified_at REAL,
    FOREIGN KEY (owner) REFERENCES users(owner) ON DELETE CASCADE
);

-- 2. Indexes for fast owner queries and status filtering
CREATE INDEX IF NOT EXISTS idx_credential_vault_owner ON credential_vault(owner);
CREATE INDEX IF NOT EXISTS idx_credential_vault_status ON credential_vault(status);
