"""VAPM - Verifiable AI Portfolio Manager API."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from config import get_settings
from db import init_db, close_db
from db.database import get_session
from db.models import Decision as DecisionModel
from services import market_data_service, feature_engine, prediction_service
from services.trade_executor import trade_executor
from services.position_manager import position_manager
from services.risk_guardian import risk_guardian
from services.blockchain_client import blockchain_client
from events import event_publisher
from sqlalchemy import select

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # ─────────────────────────────────────────────────────────────
    # STARTUP
    # ─────────────────────────────────────────────────────────────
    print(f"Starting {settings.agent_name}...")

    # Initialize database
    print("Initializing database...")
    await init_db()

    # Start market data service
    print("Starting market data service...")
    await market_data_service.start()

    # Load ML model
    print("Loading ML model...")
    if prediction_service.load_model():
        print("ML model loaded successfully")
    else:
        print("Warning: ML model not loaded - predictions unavailable")

    # Start trade executor
    print("Starting trade executor...")
    await trade_executor.start()

    # Initialize blockchain client
    print("Initializing blockchain client...")
    if await blockchain_client.initialize():
        print("Blockchain client ready")
    else:
        print("Blockchain client disabled or not configured")

    print(f"{settings.agent_name} is ready!")

    yield

    # ─────────────────────────────────────────────────────────────
    # SHUTDOWN
    # ─────────────────────────────────────────────────────────────
    print("Shutting down...")

    await trade_executor.stop()
    await market_data_service.stop()
    await blockchain_client.close()
    await close_db()

    print("Shutdown complete")


app = FastAPI(
    title="VAPM - Verifiable AI Portfolio Manager",
    description="AI trading agent with on-chain verifiable decisions",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────
# HEALTH & STATUS ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "agent": settings.agent_name,
    }


@app.get("/agent/status")
async def agent_status():
    """Get current agent status."""
    return {
        "agent_name": settings.agent_name,
        "status": "running",
        "latest_price": market_data_service.latest_price,
        "symbol": market_data_service.symbol,
        "position": None,
        "pnl_today": 0.0,
        "total_pnl": 0.0,
        "trades_today": 0,
    }


# ─────────────────────────────────────────────────────────────
# MARKET DATA ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/market/price")
async def get_current_price():
    """Get current price."""
    return {
        "symbol": market_data_service.symbol,
        "price": market_data_service.latest_price,
    }


@app.get("/market/candles")
async def get_candles(limit: int = 100):
    """Get recent candles."""
    candles = await market_data_service.get_recent_candles(limit=limit)
    return {
        "symbol": market_data_service.symbol,
        "interval": "1m",
        "count": len(candles),
        "candles": [
            {
                "open_time": c.open_time,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ],
    }


@app.get("/market/latest")
async def get_latest_market_data():
    """Get latest market data snapshot."""
    candle = market_data_service.latest_candle
    return {
        "symbol": market_data_service.symbol,
        "price": market_data_service.latest_price,
        "candle": {
            "open": candle.open if candle else None,
            "high": candle.high if candle else None,
            "low": candle.low if candle else None,
            "close": candle.close if candle else None,
            "volume": candle.volume if candle else None,
            "is_closed": candle.is_closed if candle else None,
        } if candle else None,
    }


# ─────────────────────────────────────────────────────────────
# FEATURE ENGINE ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/features/compute")
async def compute_features():
    """Compute and return current technical indicators."""
    features = await feature_engine.compute_features()
    if features is None:
        return {"error": "Not enough data to compute features"}
    return {
        "symbol": market_data_service.symbol,
        "features": features.to_dict(),
        "feature_array": features.to_array().tolist(),
    }


@app.get("/features/latest")
async def get_latest_features():
    """Get most recently computed features."""
    if feature_engine.latest_features is None:
        # Compute if not available
        features = await feature_engine.compute_features()
        if features is None:
            return {"error": "Not enough data to compute features"}
    return {
        "symbol": market_data_service.symbol,
        "features": feature_engine.latest_features.to_dict(),
    }


# ─────────────────────────────────────────────────────────────
# PREDICTION ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/predict")
async def get_prediction():
    """
    Compute features and make a prediction.

    Returns prediction direction (UP/DOWN), confidence, and SHAP explanation.
    """
    # First compute features
    features = await feature_engine.compute_features()
    if features is None:
        return {"error": "Not enough candle data to compute features"}

    # Make prediction
    prediction = await prediction_service.predict_and_publish(features)
    if prediction is None:
        return {"error": "Model not loaded"}

    return {
        "symbol": market_data_service.symbol,
        "prediction": prediction.to_dict(),
    }


@app.get("/predict/latest")
async def get_latest_prediction():
    """Get most recent prediction without recomputing."""
    if prediction_service.latest_prediction is None:
        return {"error": "No prediction available - call /predict first"}

    return {
        "symbol": market_data_service.symbol,
        "prediction": prediction_service.latest_prediction.to_dict(),
    }


@app.get("/predict/model")
async def get_model_info():
    """Get ML model metadata and status."""
    return {
        "model": prediction_service.get_model_info(),
    }


# ─────────────────────────────────────────────────────────────
# TRADING ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/trades/position")
async def get_current_position():
    """Get current trading position."""
    pos = position_manager.position
    return {
        "has_position": position_manager.has_position,
        "position": pos.to_dict() if position_manager.has_position else None,
        "capital": position_manager.capital,
    }


@app.get("/trades/history")
async def get_trade_history(limit: int = 20):
    """Get recent trade history."""
    trades = trade_executor.trade_history[-limit:]
    return {
        "count": len(trades),
        "trades": [t.to_dict() for t in trades],
    }


@app.post("/trades/close")
async def close_position(reason: str = "manual"):
    """Manually close the current position."""
    if not position_manager.has_position:
        return {"error": "No position to close"}

    price = market_data_service.latest_price
    if price is None or price == 0:
        return {"error": "No current price available"}

    result = await trade_executor.manual_close(price, reason)

    if result is None:
        return {"error": "Failed to close position"}

    return {
        "success": result.success,
        "trade": result.to_dict(),
    }


@app.get("/trades/status")
async def get_executor_status():
    """Get trade executor status."""
    return trade_executor.get_status()


# ─────────────────────────────────────────────────────────────
# RISK ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/risk/state")
async def get_risk_state():
    """Get current risk state."""
    return {
        "state": risk_guardian.state.model_dump(),
        "config": risk_guardian.get_config(),
        "trading_enabled": risk_guardian.is_trading_enabled,
    }


@app.post("/risk/circuit-breaker/reset")
async def reset_circuit_breaker():
    """Reset the circuit breaker (requires manual intervention)."""
    await risk_guardian.reset_circuit_breaker()
    return {
        "success": True,
        "trading_enabled": risk_guardian.is_trading_enabled,
    }


# ─────────────────────────────────────────────────────────────
# BLOCKCHAIN / VERIFICATION ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.get("/agent/onchain")
async def get_onchain_status():
    """Get agent's on-chain identity and blockchain status."""
    status = blockchain_client.get_status()

    # Add agent info if registered
    agent_info = None
    if status.get("initialized"):
        info = await blockchain_client.get_agent_info()
        if info:
            agent_info = {
                "pubkey": info.pubkey,
                "name": info.name,
                "decision_count": info.decision_count,
                "registered_at": info.registered_at,
                "active": info.active,
            }
        status["decision_count"] = await blockchain_client.get_decision_count()

    return {
        "blockchain": status,
        "agent": agent_info,
    }


@app.post("/agent/register")
async def register_agent_onchain(name: str = "VAPM-Alpha", metadata_uri: str = ""):
    """Register the agent on-chain (mint NFT identity)."""
    if not blockchain_client.is_enabled:
        return {"error": "Blockchain not enabled"}

    result = await blockchain_client.register_agent(name, metadata_uri)

    if result.success:
        return {
            "success": True,
            "tx_hash": result.tx_hash,
            "slot": result.slot,
            "note": result.error,  # Contains "Already registered" if applicable
        }
    return {
        "success": False,
        "error": result.error,
    }


@app.get("/verify/{decision_id}")
async def verify_decision(decision_id: str):
    """
    Verify a decision hash on-chain.

    Compares the stored decision hash in PostgreSQL against
    the on-chain record in ValidationRegistry.
    """
    # Get decision from database
    async with get_session() as session:
        result = await session.execute(
            select(DecisionModel).where(DecisionModel.id == decision_id)
        )
        decision = result.scalar_one_or_none()

    if not decision:
        return {"error": "Decision not found", "decision_id": decision_id}

    response = {
        "decision_id": decision_id,
        "timestamp": decision.timestamp.isoformat() if decision.timestamp else None,
        "action": decision.action,
        "stored_hash": decision.decision_hash,
        "price": decision.price,
        "confidence": decision.confidence,
    }

    # Verify on-chain if blockchain is enabled
    if blockchain_client.is_enabled and decision.decision_hash:
        try:
            is_valid = await blockchain_client.verify_decision(
                decision_id,
                decision.decision_hash,
            )
            onchain_record = await blockchain_client.get_validation_record(decision_id)

            response["onchain_verification"] = {
                "verified": is_valid,
                "record": {
                    "hash": onchain_record.decision_hash if onchain_record else None,
                    "confidence": onchain_record.model_confidence if onchain_record else None,
                    "risk_score": onchain_record.risk_score if onchain_record else None,
                    "timestamp": onchain_record.timestamp if onchain_record else None,
                    "executed": onchain_record.executed if onchain_record else None,
                } if onchain_record else None,
            }
        except Exception as e:
            response["onchain_verification"] = {
                "verified": False,
                "error": str(e),
            }
    else:
        response["onchain_verification"] = {
            "verified": None,
            "note": "Blockchain not enabled or no hash stored",
        }

    return response


@app.get("/decisions/onchain")
async def get_onchain_decisions():
    """Get all decisions logged on-chain for this agent."""
    if not blockchain_client.is_enabled:
        return {"error": "Blockchain not enabled"}

    count = await blockchain_client.get_decision_count()
    return {
        "agent_address": blockchain_client.address,
        "decision_count": count,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
