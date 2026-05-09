#!/usr/bin/env python3
"""
Standalone test script for Trade Executor Service.
Runs without pytest for standalone testing.
"""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def success(self, name):
        print(f"  [PASS] {name}")
        self.passed += 1

    def fail(self, name, error):
        print(f"  [FAIL] {name}: {error}")
        self.failed += 1
        self.errors.append((name, error))

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*50}")
        print(f"Results: {self.passed}/{total} passed")
        if self.errors:
            print(f"Failures:")
            for name, error in self.errors:
                print(f"  - {name}: {error}")
        return self.failed == 0


results = TestResult()


# ===== Mock Event Publisher =====
class MockEventPublisher:
    def __init__(self):
        self.published = []
        self.stored = {}

    async def connect(self): pass
    async def disconnect(self): pass

    async def publish(self, channel, data):
        self.published.append((channel, data))

    async def add_to_stream(self, stream, data, maxlen=1000):
        if stream not in self.stored:
            self.stored[stream] = []
        self.stored[stream].append(data)

    async def set_json(self, key, data, expire_seconds=None):
        self.stored[key] = data

    async def get_json(self, key):
        return self.stored.get(key)


# ===== Setup Mocks BEFORE importing services =====
def setup_global_mocks():
    """Must be called before importing any services."""
    import events.publisher as pub_module
    mock = MockEventPublisher()
    pub_module.event_publisher = mock
    return mock


# Setup mocks immediately
mock_publisher = setup_global_mocks()

# NOW import services (they will get the mocked publisher)
from services.position_manager import PositionManager, position_manager
from services.risk_guardian import RiskGuardian, RiskConfig, risk_guardian
from services.trade_executor import TradeExecutorService
from models.decision import TradeAction


# ===== Tests =====

def test_position_manager_empty():
    """Test initial empty position state."""
    pm = PositionManager(initial_capital=10000.0)
    assert not pm.has_position, "Should not have position initially"
    assert pm.position.size == 0.0, "Size should be 0"
    results.success("position_manager_empty")


def test_position_size_calculation():
    """Test position size calculation."""
    pm = PositionManager(initial_capital=10000.0)

    # 3% of $10,000 = $300 worth of ETH at $2000 = 0.15 ETH
    size = pm.calculate_position_size(price=2000.0, position_pct=0.03)
    assert abs(size - 0.15) < 0.001, f"Expected 0.15, got {size}"

    # 5% of $10,000 = $500 at $2500 = 0.2 ETH
    size = pm.calculate_position_size(price=2500.0, position_pct=0.05)
    assert abs(size - 0.2) < 0.001, f"Expected 0.2, got {size}"

    results.success("position_size_calculation")


async def test_open_close_position():
    """Test opening and closing a position."""
    pm = PositionManager(initial_capital=10000.0)

    # Open position
    pos = await pm.open_position(
        side="LONG",
        size=0.15,
        entry_price=2000.0,
        decision_id="test-1",
    )
    assert pm.has_position, "Should have position after open"
    assert pos.size == 0.15, f"Size should be 0.15, got {pos.size}"

    # Update price
    await pm.update_price(2100.0)
    pnl = pm.position.unrealized_pnl
    assert abs(pnl - 15.0) < 0.01, f"PnL should be ~15.0, got {pnl}"

    # Close position
    closed, realized = await pm.close_position(
        exit_price=2100.0,
        reason="test",
        decision_id="test-2",
    )
    assert not pm.has_position, "Should not have position after close"
    assert abs(realized - 15.0) < 0.01, f"Realized PnL should be ~15.0, got {realized}"

    results.success("open_close_position")


def test_risk_guardian_config():
    """Test risk guardian configuration."""
    rg = RiskGuardian()
    assert rg.config.max_position_size_pct == 0.05, "Max position should be 5%"
    assert rg.config.stop_loss_pct == 0.02, "Stop loss should be 2%"
    assert rg.config.take_profit_pct == 0.04, "Take profit should be 4%"
    results.success("risk_guardian_config")


async def test_risk_check_passing():
    """Test risk check that should pass."""
    rg = RiskGuardian(config=RiskConfig(min_trade_interval_seconds=0))

    result = await rg.check_trade(
        action="BUY",
        position_size_pct=0.03,
        current_exposure_pct=0.0,
    )

    assert result.can_trade, f"Should pass, violations: {result.violations}"
    assert len(result.violations) == 0, f"No violations expected: {result.violations}"
    results.success("risk_check_passing")


async def test_risk_check_position_too_large():
    """Test risk check failing for oversized position."""
    rg = RiskGuardian(config=RiskConfig(
        max_position_size_pct=0.05,
        min_trade_interval_seconds=0,
    ))

    result = await rg.check_trade(
        action="BUY",
        position_size_pct=0.10,  # 10% - exceeds 5% limit
        current_exposure_pct=0.0,
    )

    assert not result.can_trade, "Should fail for oversized position"
    assert "Position size" in str(result.violations), f"Expected position size violation: {result.violations}"
    results.success("risk_check_position_too_large")


def test_stop_loss_trigger():
    """Test stop loss trigger logic."""
    rg = RiskGuardian(config=RiskConfig(stop_loss_pct=0.02))

    assert not rg.check_stop_loss(-0.01), "-1% should not trigger"
    assert rg.check_stop_loss(-0.02), "-2% should trigger"
    assert rg.check_stop_loss(-0.05), "-5% should trigger"
    results.success("stop_loss_trigger")


def test_take_profit_trigger():
    """Test take profit trigger logic."""
    rg = RiskGuardian(config=RiskConfig(take_profit_pct=0.04))

    assert not rg.check_take_profit(0.03), "+3% should not trigger"
    assert rg.check_take_profit(0.04), "+4% should trigger"
    assert rg.check_take_profit(0.10), "+10% should trigger"
    results.success("take_profit_trigger")


async def test_entry_requires_up_direction():
    """Test entry requires UP direction."""
    executor = TradeExecutorService()

    action, reason = await executor._evaluate_entry(
        direction="DOWN",
        confidence=0.70,
        price=2000.0,
    )

    assert action == TradeAction.HOLD, f"Should HOLD on DOWN, got {action}"
    assert "DOWN" in reason, f"Reason should mention DOWN: {reason}"
    results.success("entry_requires_up_direction")


async def test_entry_requires_confidence():
    """Test entry requires sufficient confidence."""
    executor = TradeExecutorService()

    action, reason = await executor._evaluate_entry(
        direction="UP",
        confidence=0.50,  # Below 0.60 threshold
        price=2000.0,
    )

    assert action == TradeAction.HOLD, f"Should HOLD on low confidence, got {action}"
    assert "Confidence" in reason, f"Reason should mention confidence: {reason}"
    results.success("entry_requires_confidence")


async def test_full_entry_flow():
    """Test full entry conditions with passing criteria."""
    executor = TradeExecutorService()

    # Reset risk guardian state for clean test
    risk_guardian._state.last_trade_timestamp = None
    risk_guardian._state.trades_today = 0

    action, reason = await executor._evaluate_entry(
        direction="UP",
        confidence=0.70,  # Above 0.60 threshold
        price=2000.0,
    )

    assert action == TradeAction.BUY, f"Should BUY on good signal, got {action}. Reason: {reason}"
    results.success("full_entry_flow")


def test_exit_stop_loss_logic():
    """Test stop loss detection logic."""
    rg = RiskGuardian(config=RiskConfig(stop_loss_pct=0.02))

    # -2.5% unrealized loss should trigger stop loss
    pnl_pct = -0.025
    should_exit = rg.check_stop_loss(pnl_pct)

    assert should_exit, f"Should trigger stop loss at {pnl_pct:.1%}"
    results.success("exit_stop_loss_logic")


def test_exit_take_profit_logic():
    """Test take profit detection logic."""
    rg = RiskGuardian(config=RiskConfig(take_profit_pct=0.04))

    # +5% profit should trigger take profit
    pnl_pct = 0.05
    should_exit = rg.check_take_profit(pnl_pct)

    assert should_exit, f"Should trigger take profit at {pnl_pct:.1%}"
    results.success("exit_take_profit_logic")


def test_position_age_limit():
    """Test position age limit."""
    rg = RiskGuardian(config=RiskConfig(max_position_age_seconds=1800))

    # 35 minutes should trigger age limit (1800s = 30min)
    age_seconds = 35 * 60
    should_exit = rg.check_position_age(age_seconds)

    assert should_exit, f"Should trigger age limit at {age_seconds}s"
    results.success("position_age_limit")


# ===== Run All Tests =====

async def run_async_tests():
    """Run all async tests."""
    await test_open_close_position()
    await test_risk_check_passing()
    await test_risk_check_position_too_large()
    await test_entry_requires_up_direction()
    await test_entry_requires_confidence()
    await test_full_entry_flow()


def main():
    print("="*50)
    print("Trade Executor Service Tests")
    print("="*50)

    # Sync tests
    print("\n[Position Manager]")
    try:
        test_position_manager_empty()
        test_position_size_calculation()
    except Exception as e:
        results.fail("position_manager", str(e))

    # Risk guardian tests
    print("\n[Risk Guardian]")
    try:
        test_risk_guardian_config()
        test_stop_loss_trigger()
        test_take_profit_trigger()
        test_exit_stop_loss_logic()
        test_exit_take_profit_logic()
        test_position_age_limit()
    except Exception as e:
        results.fail("risk_guardian", str(e))

    # Async tests
    print("\n[Trade Flow Tests]")
    try:
        asyncio.run(run_async_tests())
    except Exception as e:
        import traceback
        traceback.print_exc()
        results.fail("async_tests", str(e))

    # Summary
    success = results.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
