# VAPM - Verifiable AI Portfolio Manager

> An autonomous AI trading agent with **encrypted strategy** (Encrypt FHE) and **cryptographically enforced risk limits** (Ika dWallet) on Solana.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## The Problem

AI trading agents have two critical vulnerabilities:

1. **Front-running**: Trading signals are visible on-chain. Anyone can read the agent's prediction and trade ahead of it.
2. **Bypassable guardrails**: Risk limits are enforced in software. A malicious operator can modify the code and bypass position size limits, loss caps, or drawdown protection.

## The Solution

VAPM uses two new Solana primitives to solve both problems:

- **Encrypt (FHE)**: The agent's confidence scores and risk metrics are encrypted using Fully Homomorphic Encryption before on-chain storage. Nobody can read the trading signals -- not validators, not indexers, not competing traders.

- **Ika (dWallet)**: The agent's trading wallet is a distributed MPC key controlled by an on-chain program. Risk limits are stored on-chain and checked before every trade. The dWallet **physically cannot sign** a transaction that violates risk limits -- not even the agent operator can bypass them.

## How It Works

```
                          VAPM Architecture
+----------------------------------------------------------------+
|                                                                |
|  Market Data -> Feature Engine -> XGBoost ML -> Strategy       |
|  (Birdeye/Jupiter)                                |            |
|                                                   v            |
|                                          [Encrypt FHE]         |
|                                          Confidence + risk     |
|                                          scores encrypted      |
|                                                   |            |
|                                                   v            |
|  +------------------------------------------+                  |
|  |     On-Chain Risk Enforcement (Anchor)    |                  |
|  |  position_size <= max_position_bps?       |                  |
|  |  daily_loss    <= max_daily_loss_bps?      |                  |
|  |  drawdown     <= max_drawdown_bps?         |                  |
|  +------------------------------------------+                  |
|           |                          |                          |
|         PASS                       FAIL                         |
|           |                          |                          |
|    [Ika dWallet]              [TradeRejected]                   |
|    approve_message             event emitted                    |
|    MPC signing                 trade blocked                    |
|           |                                                     |
|           v                                                     |
|    Jupiter Aggregator                                           |
|    SOL/USDC swap                                                |
+----------------------------------------------------------------+
```

## How VAPM Uses Encrypt

Encrypt enables smart contracts to **compute on encrypted data** without decrypting it. VAPM uses this to keep trading signals private:

1. Before logging a decision on-chain, the agent encrypts `confidence` and `risk_score` via Encrypt's gRPC API
2. Encrypted ciphertext accounts are created on Solana (owned by the Encrypt program)
3. The on-chain decision record stores references to ciphertext accounts, not plaintext values
4. FHE computation graphs (`check_position_limit`, `update_cumulative_pnl`) can run risk checks on encrypted data
5. Only aggregate performance metrics (total PnL, win rate) are ever decrypted -- individual signals remain private

**Result**: The agent's alpha is protected. Observers see that a decision was made but cannot determine the direction or confidence.

## How VAPM Uses Ika

Ika provides **dWallets** -- distributed signing keys controlled by Solana programs via 2PC-MPC. VAPM uses this for unbypassable risk enforcement:

1. A dWallet is created for the agent (Curve25519 for Solana-native signing)
2. The dWallet's authority is transferred to the VAPM program's CPI authority PDA
3. When the agent wants to trade, it calls the `approve_trade` instruction
4. The on-chain program checks risk limits stored in the `AgentState` PDA
5. Only if ALL limits pass, the program CPI-calls `approve_message` on the dWallet
6. The Ika network produces the signature via 2PC-MPC
7. The signed transaction is broadcast to execute the swap

**Result**: Risk limits are cryptographic, not just software. The dWallet won't sign without program approval.

## Tech Stack

| Layer | Technology |
|-------|------------|
| ML | XGBoost, SHAP, scikit-learn |
| Backend | Python 3.11, FastAPI |
| Database | PostgreSQL + TimescaleDB, Redis |
| Blockchain | Anchor (Rust), solana-py, solders |
| Privacy | Encrypt FHE (`encrypt-anchor`) |
| Custody | Ika dWallet (`ika-dwallet-anchor`) |
| DEX | Jupiter Aggregator API |

## Quick Start

### Prerequisites

- Python 3.11+
- Docker & Docker Compose
- Solana CLI + Anchor CLI
- Solana Devnet SOL (`solana airdrop 2`)

### Setup

```bash
git clone https://github.com/yourusername/vapm.git
cd vapm
cp .env.example .env

# Infrastructure
docker-compose up -d postgres redis

# Python deps
pip install -r requirements.txt

# Solana wallet
solana-keygen new -o ~/.config/solana/id.json
solana config set --url devnet
solana airdrop 2

# Deploy program
anchor build && anchor deploy
# Copy program ID to .env as DECISION_PROGRAM_ID

# Optional: setup dWallet
python scripts/setup_dwallet.py

# Run
cd backend && python -m uvicorn main:app --reload
```

## On-Chain Program

### vapm_decisions (Anchor/Rust)

| Instruction | Purpose |
|-------------|---------|
| `initialize_agent` | Create agent PDA with on-chain risk limits |
| `set_risk_limits` | Update max position/loss/drawdown (authority-only) |
| `set_dwallet` | Link a dWallet for MPC custody |
| `approve_trade` | Check risk limits, approve via dWallet CPI if configured |
| `reject_trade` | Log rejection for audit trail |
| `log_decision` | Store decision hash on-chain |
| `mark_executed` | Flag decision as executed |

Risk limits stored on-chain (basis points):
- `max_position_bps` (default: 500 = 5%)
- `max_daily_loss_bps` (default: 300 = 3%)
- `max_drawdown_bps` (default: 1000 = 10%)

## API Endpoints

```
GET  /health                  # System health
GET  /agent/status            # Agent state, PnL
GET  /agent/onchain           # Solana identity and decisions
GET  /agent/dwallet           # dWallet custody status + risk limits
GET  /agent/encrypt           # Encrypt FHE status + encrypted decisions
GET  /market/price            # Current SOL/USDC price
GET  /predict                 # ML prediction with SHAP
GET  /trades/position         # Current position
GET  /risk/state              # Risk metrics
POST /agent/register          # Register agent on-chain
GET  /verify/{decision_id}    # Verify decision hash
```

## Project Structure

```
vapm/
+-- programs/vapm_decisions/     # Anchor program
|   +-- src/lib.rs               # Risk limits, approve_trade, Ika CPI
|   +-- src/encrypt_fns.rs       # FHE computation graphs
+-- backend/
|   +-- services/
|   |   +-- trade_executor.py    # Trading orchestrator
|   |   +-- blockchain_client.py # Solana + Jupiter integration
|   |   +-- dwallet_client.py    # Ika dWallet management
|   |   +-- encrypt_client.py    # Encrypt FHE privacy
|   |   +-- risk_guardian.py     # Risk management engine
|   |   +-- prediction_service.py # XGBoost + SHAP
|   +-- models/                  # Pydantic data models
|   +-- core/                    # Technical indicators
+-- ml/                          # ML training pipeline
+-- scripts/                     # Setup and utilities
```

## Target Users

- **AI Agent Operators**: Need provable, unbypassable risk controls for autonomous trading
- **Fund Managers**: Want strategy privacy (no front-running) with transparent risk reporting
- **Institutional DeFi**: Require custody solutions where risk limits are cryptographically enforced

## License

MIT License - see [LICENSE](LICENSE) for details.

---

Built for the **Frontier Hackathon** (Encrypt + Ika track) on Superteam Earn.
