"""Configuration management for VAPM."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database (using non-default ports to avoid conflicts)
    database_url: str = "postgresql://postgres:postgres@localhost:5433/vapm"
    redis_url: str = "redis://localhost:6380"

    # Solana
    solana_rpc_url: str = "https://api.devnet.solana.com"
    solana_ws_url: str = "wss://api.devnet.solana.com"
    agent_keypair_path: str = ""
    decision_program_id: str = ""
    blockchain_enabled: bool = False

    # Jupiter
    jupiter_api_url: str = "https://api.jup.ag"

    # Token Mints (Solana Devnet)
    sol_mint: str = "So11111111111111111111111111111111111111112"
    usdc_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    # Ika dWallet
    ika_grpc_url: str = "pre-alpha-dev-1.ika.ika-network.net:443"
    ika_program_id: str = "87W54kGYFQ1rgWqMeu4XTPHWXWmXSQCcjm8vCTfiq1oY"
    dwallet_address: str = ""

    # Encrypt FHE
    encrypt_grpc_url: str = "pre-alpha-dev-1.encrypt.ika-network.net:443"
    encrypt_program_id: str = "4ebfzWdKnrnGseuQpezXdG8yCdHqwQ1SSBHD3bWArND8"

    # Market Data
    birdeye_api_key: str = ""

    # Agent Config
    agent_name: str = "VAPM-Alpha"
    max_position_size: float = 0.05  # 5% of capital
    max_daily_loss: float = 0.03     # 3% of capital
    max_drawdown: float = 0.10       # 10% of capital
    trade_interval_seconds: int = 60

    # ML Model
    model_path: str = "ml/models/xgb_v1.joblib"
    prediction_threshold: float = 0.65
    confidence_threshold: float = 0.55

    # API (using 8001 to avoid conflict with other projects)
    api_host: str = "0.0.0.0"
    api_port: int = 8001

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        protected_namespaces = ('settings_',)


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
