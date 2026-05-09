"""
Trade Executor Service - Orchestrates trading decisions.

This is the main trading brain that:
1. Subscribes to prediction events
2. Evaluates entry/exit conditions
3. Runs risk checks
4. Executes (simulated) trades
5. Logs everything for audit
"""

import uuid
import json
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, asdict

from events.subscriber import event_subscriber
from events.publisher import event_publisher
from services.position_manager import position_manager, PositionState
from services.risk_guardian import risk_guardian
from services.blockchain_client import blockchain_client
from services.feature_engine import feature_engine
from models.decision import TradeAction, DecisionRecord, MarketState, ModelOutput, StrategyDecision, RiskValidation
from db.database import get_session
from db.models import Decision as DecisionModel, TradeExecution as TradeExecutionModel


# Entry/exit thresholds
ENTRY_CONFIDENCE_THRESHOLD = 0.60
EXIT_REVERSAL_CONFIDENCE = 0.55
BASE_POSITION_SIZE_PCT = 0.03  # Base 3% position size (scaled by volatility)


@dataclass
class TradeResult:
    """Result of a trade execution."""
    success: bool
    trade_id: Optional[str]
    decision_id: str
    action: str
    amount: float
    price: float
    reason: str
    pnl: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


class TradeExecutorService:
    """
    Main trading orchestrator.

    Listens to predictions and decides whether to:
    - Open a new position (BUY)
    - Close existing position (SELL)
    - Hold (do nothing)
    """

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_prediction: Optional[dict] = None
        self.trade_history: list[TradeResult] = []

    async def start(self):
        """Start the trade executor service."""
        # Load state from Redis
        await event_publisher.connect()
        await event_subscriber.connect()
        await position_manager.load_from_redis()
        await risk_guardian.load_state()

        # Register prediction handler
        event_subscriber.on("event:prediction_ready", self._on_prediction)

        # Start subscriber
        await event_subscriber.start()

        self._running = True
        print("TradeExecutorService started")
        print(f"  Entry confidence threshold: {ENTRY_CONFIDENCE_THRESHOLD}")
        print(f"  Base position size: {BASE_POSITION_SIZE_PCT:.0%} (volatility-scaled)")
        print(f"  Has position: {position_manager.has_position}")

    async def stop(self):
        """Stop the trade executor service."""
        self._running = False
        await event_subscriber.disconnect()
        print("TradeExecutorService stopped")

    async def _on_prediction(self, prediction: dict):
        """
        Handle incoming prediction event.

        This is the main decision loop.
        """
        self._last_prediction = prediction

        timestamp = prediction.get("timestamp", 0)
        price = prediction.get("price", 0.0)
        direction = prediction.get("direction", "")
        confidence = prediction.get("confidence", 0.0)
        shap = prediction.get("shap_explanation", {})

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Prediction: {direction} ({confidence:.1%}) @ ${price:.2f}")

        # Update position price if we have one
        if position_manager.has_position:
            await position_manager.update_price(price)

        # Evaluate what to do (now returns position_size for dynamic sizing)
        action, reason, position_size = await self._evaluate_action(direction, confidence, price)

        if action == TradeAction.HOLD:
            print(f"  -> HOLD: {reason}")
            return

        # Build decision record with dynamic position size
        decision = await self._build_decision(
            price=price,
            direction=direction,
            confidence=confidence,
            shap=shap,
            action=action,
            reason=reason,
            position_size_pct=position_size,
        )

        # Execute the trade with dynamic position size
        result = await self._execute_trade(decision, price, position_size)

        # Log result
        if result.success:
            print(f"  -> {result.action}: {result.amount:.6f} SOL @ ${result.price:.2f}")
            if result.pnl is not None:
                print(f"     PnL: ${result.pnl:.2f}")
        else:
            print(f"  -> FAILED: {result.reason}")

    async def _evaluate_action(
        self,
        direction: str,
        confidence: float,
        price: float,
    ) -> tuple[TradeAction, str, float]:
        """
        Evaluate what trading action to take.

        Returns:
            Tuple of (action, reason, position_size_pct)
        """
        # Check if we have a position
        if position_manager.has_position:
            return await self._evaluate_exit(direction, confidence, price)
        else:
            return await self._evaluate_entry(direction, confidence, price)

    async def _evaluate_entry(
        self,
        direction: str,
        confidence: float,
        price: float,
    ) -> tuple[TradeAction, str, float]:
        """
        Evaluate entry conditions with dynamic position sizing.

        Returns:
            Tuple of (action, reason, position_size_pct)
        """
        # Must be bullish with high confidence
        if direction != "UP":
            return TradeAction.HOLD, "Direction is DOWN (long-only strategy)", 0.0

        if confidence < ENTRY_CONFIDENCE_THRESHOLD:
            return TradeAction.HOLD, f"Confidence {confidence:.1%} below threshold {ENTRY_CONFIDENCE_THRESHOLD:.0%}", 0.0

        # Get current features for volatility-scaled position sizing
        features = feature_engine.latest_features
        volatility = features.volatility if features else 0.02

        # Calculate dynamic position size (volatility-scaled with throttling)
        position_size = risk_guardian.calculate_position_size(
            base_size_pct=BASE_POSITION_SIZE_PCT,
            current_volatility=volatility,
        )

        # Check if throttled to zero (drawdown too high)
        if position_size < 0.005:  # Less than 0.5%
            throttle_factor = risk_guardian.get_throttle_factor()
            return TradeAction.HOLD, f"Position size throttled to {position_size:.1%} (throttle: {throttle_factor:.0%})", 0.0

        # Check risk limits with dynamic position size
        risk_result = await risk_guardian.check_trade(
            action="BUY",
            position_size_pct=position_size,
            current_exposure_pct=0.0,
        )

        if not risk_result.can_trade:
            return TradeAction.HOLD, f"Risk check failed: {', '.join(risk_result.violations)}", 0.0

        return TradeAction.BUY, f"Entry signal: {direction} with {confidence:.1%} confidence (size: {position_size:.1%})", position_size

    async def _evaluate_exit(
        self,
        direction: str,
        confidence: float,
        price: float,
    ) -> tuple[TradeAction, str, float]:
        """
        Evaluate exit conditions with ATR-based dynamic stop-loss.

        Returns:
            Tuple of (action, reason, position_size_pct)
        """
        pos = position_manager.position
        pnl_pct = pos.unrealized_pnl_pct

        # Get current features for ATR-based stop-loss
        features = feature_engine.latest_features
        atr = features.atr if features else 0.0

        # 1. Dynamic ATR-based stop loss check (if ATR available)
        if atr > 0:
            if risk_guardian.check_stop_loss_dynamic(pos.entry_price, price, atr):
                stop_price = risk_guardian.calculate_stop_loss_price(pos.entry_price, atr)
                return TradeAction.SELL, f"ATR stop-loss triggered at ${price:.2f} (stop: ${stop_price:.2f})", 0.0
        else:
            # Fallback to percentage-based stop loss
            if risk_guardian.check_stop_loss(pnl_pct):
                return TradeAction.SELL, f"Stop loss triggered at {pnl_pct:.1%}", 0.0

        # 2. Take profit check
        if risk_guardian.check_take_profit(pnl_pct):
            return TradeAction.SELL, f"Take profit triggered at {pnl_pct:.1%}", 0.0

        # 3. Reversal signal check
        if direction == "DOWN" and confidence >= EXIT_REVERSAL_CONFIDENCE:
            return TradeAction.SELL, f"Reversal signal: DOWN with {confidence:.1%} confidence", 0.0

        # 4. Time decay check
        age_seconds = position_manager.get_position_age_seconds()
        if risk_guardian.check_position_age(age_seconds):
            return TradeAction.SELL, f"Position age {age_seconds/60:.0f}min exceeds limit", 0.0

        return TradeAction.HOLD, f"Holding position (PnL: {pnl_pct:+.1%})", 0.0

    async def _build_decision(
        self,
        price: float,
        direction: str,
        confidence: float,
        shap: dict,
        action: TradeAction,
        reason: str,
        position_size_pct: float = 0.0,
    ) -> DecisionRecord:
        """Build a complete decision record with dynamic position sizing."""
        decision_id = str(uuid.uuid4())

        # Build market state from feature engine if available
        features = feature_engine.latest_features
        market_state = MarketState(
            price=price,
            rsi=features.rsi if features else 50.0,
            macd=features.macd if features else 0.0,
            macd_signal=features.macd_signal if features else 0.0,
            ema_ratio=features.ema_ratio if features else 1.0,
            volatility=features.volatility if features else 0.02,
            volume_spike=features.volume_spike if features else 1.0,
            momentum=features.momentum if features else 0.0,
            bollinger_position=features.bollinger_position if features else 0.5,
        )

        # Model output
        model_output = ModelOutput(
            probability_up=confidence if direction == "UP" else 1 - confidence,
            confidence=confidence,
            shap_values={k: v.get("value", 0) for k, v in shap.items()},
        )

        # Strategy decision with dynamic position size
        strategy_decision = StrategyDecision(
            action=action,
            reason=reason,
            position_size_pct=position_size_pct if action == TradeAction.BUY else 0.0,
        )

        # Risk validation with dynamic position size
        risk_result = await risk_guardian.check_trade(
            action=action.value,
            position_size_pct=position_size_pct,
            current_exposure_pct=position_manager.position.value_usd / position_manager.capital if position_manager.has_position else 0.0,
        )

        risk_validation = RiskValidation(
            passed=risk_result.can_trade,
            portfolio_exposure_pct=risk_guardian.state.total_exposure_pct,
            daily_pnl_pct=risk_guardian.state.daily_pnl_pct,
            current_drawdown_pct=risk_guardian.state.current_drawdown_pct,
            risk_score=risk_result.risk_score,
            violations=risk_result.violations,
        )

        decision = DecisionRecord(
            id=decision_id,
            timestamp=datetime.now(timezone.utc),
            asset="SOL/USDC",
            market_state=market_state,
            model_output=model_output,
            strategy_decision=strategy_decision,
            risk_validation=risk_validation,
        )

        return decision

    async def _execute_trade(
        self,
        decision: DecisionRecord,
        price: float,
        position_size_pct: float = BASE_POSITION_SIZE_PCT,
    ) -> TradeResult:
        """
        Execute the trade with on-chain risk enforcement.

        Flow:
        1. Compute decision hash
        2. Request on-chain trade approval (risk limit check)
        3. Log decision hash on-chain (if blockchain enabled)
        4. Execute trade (simulated position update)
        5. Mark decision as executed on-chain
        6. Save to PostgreSQL
        7. Update equity tracking for drawdown management
        """
        action = decision.strategy_decision.action
        decision_hash = decision.compute_hash()
        decision.trade_intent_hash = decision_hash

        # ─────────────────────────────────────────────────────────────
        # STEP 0: On-chain risk enforcement (dWallet guardrails)
        # ─────────────────────────────────────────────────────────────
        if blockchain_client.is_enabled:
            try:
                position_bps = int(position_size_pct * 10000)
                exposure_bps = int(
                    (position_manager.position.value_usd / position_manager.capital * 10000)
                    if position_manager.has_position else 0
                )
                daily_pnl_bps = int(abs(risk_guardian.state.daily_pnl_pct) * 10000)
                drawdown_bps = int(risk_guardian.state.current_drawdown_pct * 10000)

                hash_bytes = bytes.fromhex(
                    decision_hash[2:] if decision_hash.startswith("0x") else decision_hash
                )

                approval = await blockchain_client.approve_trade(
                    position_size_bps=position_bps,
                    current_exposure_bps=exposure_bps,
                    daily_pnl_bps=daily_pnl_bps,
                    current_drawdown_bps=drawdown_bps,
                    message_hash=hash_bytes,
                )

                if not approval.success:
                    # Trade rejected by on-chain risk limits
                    await blockchain_client.reject_trade(
                        position_size_bps=position_bps,
                        daily_pnl_bps=daily_pnl_bps,
                        current_drawdown_bps=drawdown_bps,
                        reason=approval.error or "On-chain risk check failed",
                    )
                    print(f"  [Guardrail] REJECTED: {approval.error}")
                    return TradeResult(
                        success=False,
                        trade_id=None,
                        decision_id=decision.id,
                        action=action.value,
                        amount=0.0,
                        price=price,
                        reason=f"On-chain risk rejection: {approval.error}",
                    )

                print(f"  [Guardrail] Approved: pos={position_bps}bps, dd={drawdown_bps}bps")
            except Exception as e:
                print(f"  [Guardrail] Check error (proceeding): {e}")

        # ─────────────────────────────────────────────────────────────
        # STEP 1: Log decision hash on-chain (before execution)
        # ─────────────────────────────────────────────────────────────
        validation_tx_hash = None
        if blockchain_client.is_enabled:
            try:
                confidence_scaled = int(decision.model_output.confidence * 1000)
                risk_score_scaled = int(decision.risk_validation.risk_score * 1000)

                result = await blockchain_client.log_decision(
                    decision_id=decision.id,
                    decision_hash=decision_hash,
                    confidence=confidence_scaled,
                    risk_score=risk_score_scaled,
                )

                if result.success:
                    validation_tx_hash = result.tx_hash
                    tx_display = result.tx_hash[:16] if result.tx_hash else "local"
                    print(f"  [Chain] Decision logged: {tx_display}...")
                else:
                    print(f"  [Chain] Log failed: {result.error}")
            except Exception as e:
                print(f"  [Chain] Log error: {e}")

        # Calculate trade size using dynamic position_size_pct
        if action == TradeAction.BUY:
            size = position_manager.calculate_position_size(price, position_size_pct)

            try:
                await position_manager.open_position(
                    side="LONG",
                    size=size,
                    entry_price=price,
                    decision_id=decision.id,
                )
            except ValueError as e:
                return TradeResult(
                    success=False,
                    trade_id=None,
                    decision_id=decision.id,
                    action="BUY",
                    amount=size,
                    price=price,
                    reason=str(e),
                )

            # Record the trade with current capital for equity tracking
            current_capital = position_manager.get_current_capital()
            await risk_guardian.record_trade(current_capital=current_capital)

            # Save to database
            await self._save_decision(decision)
            trade_id = await self._save_trade_execution(decision, "BUY", size, price)

            result = TradeResult(
                success=True,
                trade_id=trade_id,
                decision_id=decision.id,
                action="BUY",
                amount=size,
                price=price,
                reason=decision.strategy_decision.reason,
            )

        elif action == TradeAction.SELL:
            if not position_manager.has_position:
                return TradeResult(
                    success=False,
                    trade_id=None,
                    decision_id=decision.id,
                    action="SELL",
                    amount=0.0,
                    price=price,
                    reason="No position to close",
                )

            pos = position_manager.position
            size = pos.size

            closed_pos, realized_pnl = await position_manager.close_position(
                exit_price=price,
                reason=decision.strategy_decision.reason,
                decision_id=decision.id,
            )

            # Update capital with realized PnL
            position_manager.update_capital(realized_pnl)

            # Record trade with PnL and updated capital for equity/drawdown tracking
            pnl_pct = realized_pnl / position_manager.capital
            current_capital = position_manager.get_current_capital()
            await risk_guardian.record_trade(pnl=pnl_pct, current_capital=current_capital)

            # Save to database
            await self._save_decision(decision)
            trade_id = await self._save_trade_execution(decision, "SELL", size, price, realized_pnl)

            result = TradeResult(
                success=True,
                trade_id=trade_id,
                decision_id=decision.id,
                action="SELL",
                amount=size,
                price=price,
                reason=decision.strategy_decision.reason,
                pnl=realized_pnl,
            )

        else:
            return TradeResult(
                success=False,
                trade_id=None,
                decision_id=decision.id,
                action="HOLD",
                amount=0.0,
                price=price,
                reason="No action taken",
            )

        # ─────────────────────────────────────────────────────────────
        # STEP 2: Mark decision as executed on-chain
        # ─────────────────────────────────────────────────────────────
        if blockchain_client.is_enabled:
            try:
                mark_result = await blockchain_client.mark_executed(decision.id)
                if mark_result.success:
                    tx_display = mark_result.tx_hash[:16] if mark_result.tx_hash else "local"
                    print(f"  [Chain] Marked executed: {tx_display}...")
                else:
                    print(f"  [Chain] Mark failed: {mark_result.error}")
            except Exception as e:
                print(f"  [Chain] Mark error: {e}")

        # Add to history
        self.trade_history.append(result)

        # Publish trade event
        await event_publisher.publish(
            "event:trade_executed",
            result.to_dict(),
        )

        return result

    async def _save_decision(self, decision: DecisionRecord):
        """Save decision to PostgreSQL."""
        try:
            # Convert timezone-aware to naive UTC for database
            timestamp = decision.timestamp.replace(tzinfo=None) if decision.timestamp.tzinfo else decision.timestamp

            async with get_session() as session:
                db_decision = DecisionModel(
                    id=decision.id,
                    timestamp=timestamp,
                    asset=decision.asset,
                    price=decision.market_state.price,
                    rsi=decision.market_state.rsi,
                    macd=decision.market_state.macd,
                    volatility=decision.market_state.volatility,
                    probability_up=decision.model_output.probability_up,
                    confidence=decision.model_output.confidence,
                    shap_values_json=json.dumps(decision.model_output.shap_values),
                    action=decision.strategy_decision.action.value,
                    reason=decision.strategy_decision.reason,
                    position_size_pct=decision.strategy_decision.position_size_pct,
                    risk_passed=decision.risk_validation.passed,
                    risk_score=decision.risk_validation.risk_score,
                    risk_violations_json=json.dumps(decision.risk_validation.violations),
                    execution_status="executed",
                    decision_hash=decision.trade_intent_hash,
                )
                session.add(db_decision)
                await session.flush()  # Ensure the insert is sent
            print(f"  [DB] Decision {decision.id[:8]} saved")
        except Exception as e:
            import traceback
            print(f"Failed to save decision: {e}")
            traceback.print_exc()

    async def _save_trade_execution(
        self,
        decision: DecisionRecord,
        action: str,
        amount: float,
        price: float,
        pnl: float = 0.0,
    ) -> str:
        """Save trade execution to PostgreSQL."""
        trade_id = str(uuid.uuid4())

        try:
            # Use naive UTC timestamp for database compatibility
            timestamp = datetime.utcnow()

            async with get_session() as session:
                trade = TradeExecutionModel(
                    id=trade_id,
                    decision_id=decision.id,
                    timestamp=timestamp,
                    asset="SOL",
                    action=action,
                    amount=amount,
                    price=price,
                    value_usd=amount * price,
                    status="executed",
                    slippage_bps=0,  # Simulated - no slippage
                )
                session.add(trade)
                await session.flush()  # Ensure the insert is sent
            print(f"  [DB] Trade {trade_id[:8]} saved")
        except Exception as e:
            print(f"Failed to save trade: {e}")

        return trade_id

    async def manual_close(self, price: float, reason: str = "manual") -> Optional[TradeResult]:
        """
        Manually close the current position.

        Args:
            price: Current market price
            reason: Reason for closing

        Returns:
            TradeResult or None if no position
        """
        if not position_manager.has_position:
            return None

        decision = await self._build_decision(
            price=price,
            direction="DOWN",
            confidence=1.0,
            shap={},
            action=TradeAction.SELL,
            reason=f"Manual close: {reason}",
            position_size_pct=0.0,  # Sell has no position size
        )

        return await self._execute_trade(decision, price, position_size_pct=0.0)

    def get_status(self) -> dict:
        """Get current executor status with risk metrics."""
        state = risk_guardian.state
        return {
            "running": self._running,
            "has_position": position_manager.has_position,
            "position": position_manager.position.to_dict() if position_manager.has_position else None,
            "capital": {
                "current": position_manager.get_current_capital(),
                "base": position_manager.capital,
                "peak": state.peak_capital,
            },
            "risk": {
                "current_drawdown_pct": state.current_drawdown_pct,
                "max_drawdown_pct": state.max_drawdown_pct,
                "throttle_factor": risk_guardian.get_throttle_factor(),
                "trading_enabled": risk_guardian.is_trading_enabled,
            },
            "trades_today": state.trades_today,
            "daily_pnl_pct": state.daily_pnl_pct,
            "last_prediction": self._last_prediction,
            "recent_trades": [t.to_dict() for t in self.trade_history[-10:]],
        }


# Global singleton
trade_executor = TradeExecutorService()
