# VAPM - Verifiable AI Portfolio Manager

> Autonomous AI trading agent with **encrypted risk enforcement** (Encrypt FHE) and **distributed custody** (Ika dWallet) on Solana.

**Track:** Encrypt + Ika (Hybrid Solutions) | **Hackathon:** Frontier Hackathon (Superteam Earn)

| | |
|---|---|
| **VAPM Program** | [`6xDo2r8Edvu1MHxwUtqmmzm3Auavf2fokbjGoJHxcMLx`](https://explorer.solana.com/address/6xDo2r8Edvu1MHxwUtqmmzm3Auavf2fokbjGoJHxcMLx?cluster=devnet) |
| **Ika dWallet Program** | [`87W54kGYFQ1rgWqMeu4XTPHWXWmXSQCcjm8vCTfiq1oY`](https://explorer.solana.com/address/87W54kGYFQ1rgWqMeu4XTPHWXWmXSQCcjm8vCTfiq1oY?cluster=devnet) |
| **Encrypt Program** | [`4ebfzWdKnrnGseuQpezXdG8yCdHqwQ1SSBHD3bWArND8`](https://explorer.solana.com/address/4ebfzWdKnrnGseuQpezXdG8yCdHqwQ1SSBHD3bWArND8?cluster=devnet) |
| **Created dWallet** | [`7ruuv1nVgmTiNaPXvQtYRf5DQLjtPb8jH9ekcrmSM15o`](https://explorer.solana.com/address/7ruuv1nVgmTiNaPXvQtYRf5DQLjtPb8jH9ekcrmSM15o?cluster=devnet) |

---

## The Problem

AI trading agents today suffer from three fundamental vulnerabilities:

1. **Visible risk parameters.** Position limits, loss caps, and drawdown thresholds are stored as plaintext on-chain. Any observer can read the agent's risk profile and exploit its boundaries.

2. **Bypassable guardrails.** Risk limits enforced in software can be modified by a malicious operator. There is no cryptographic guarantee that limits are actually respected.

3. **Centralized custody.** The agent's private key is a single point of compromise. Whoever holds it can sign any transaction, regardless of risk state.

## The Solution

VAPM eliminates all three problems by combining two Solana-native primitives:

- **Encrypt (FHE)** encrypts trade parameters AND risk limits before on-chain storage. The program compares encrypted values using Fully Homomorphic Encryption -- computing `encrypted_trade_param <= encrypted_max_limit` without ever decrypting either side. Only the boolean result (pass/fail) is revealed.

- **Ika (dWallet)** holds the agent's signing key as a distributed 2PC-MPC key. No single party possesses the private key. The on-chain program must explicitly call `approve_message` before the Ika network will produce a signature. If risk checks fail, the key physically cannot sign.

Neither primitive alone is sufficient. Without Encrypt, risk limits are readable on-chain. Without Ika, a compromised key bypasses all on-chain logic. Without the AI model, no trade proposals flow through the system. All three components are structurally required.

---

## Architecture

```
  SOL/USDC Price Feed
        |
        v
  Feature Engine --> XGBoost ML --> Trade Proposal
                                        |
                                        v
                                 [Encrypt FHE]
                                 Trade params encrypted:
                                 - position_size_bps
                                 - daily_pnl_bps
                                 - drawdown_bps
                                        |
                                        v
  +-------------------------------------------------------------+
  |              VAPM On-Chain Program (Anchor)                  |
  |                                                              |
  |  submit_trade: store encrypted param refs + message hash     |
  |                                                              |
  |  execute_risk_check:                                         |
  |    CPI -> Encrypt execute_graph (disc=4)                     |
  |    encrypted_position <= encrypted_max_position?             |
  |    encrypted_daily_pnl <= encrypted_max_daily_loss?          |
  |    encrypted_drawdown <= encrypted_max_drawdown?             |
  |                                                              |
  |  finalize_trade:                                             |
  |    Read decrypted boolean result                             |
  |    If ALL 3 checks pass:                                     |
  |      CPI -> Ika approve_message (disc=8)                     |
  |      Creates MessageApproval PDA                             |
  |    Else:                                                     |
  |      Emit TradeRejected event                                |
  +-------------------------------------------------------------+
        |                                    |
      PASS                                 FAIL
        |                                    |
  Ika 2PC-MPC signing               Trade blocked
        |
        v
  Jupiter Aggregator
  SOL/USDC swap executed
```

Key insight: the actual risk limit values and trade parameter values are NEVER visible on-chain. Validators, indexers, and competing traders see only ciphertext account references. The only information that leaves the encrypted domain is a single boolean per check.

---

## How Encrypt Is Used

Encrypt enables Fully Homomorphic Encryption computation inside Solana programs.

1. The agent encrypts trade parameters (position size, daily PnL, drawdown) via Encrypt's API before submitting them on-chain
2. Risk limits are ALSO encrypted and stored as ciphertext account references in the AgentState PDA during initialization
3. The `execute_risk_check` instruction performs CPI to Encrypt's `execute_graph` (discriminator = 4) to run a comparison computation graph: `encrypted_param <= encrypted_limit`
4. The result is an encrypted boolean, which is then decrypted via `request_decryption`
5. `finalize_trade` reads the decrypted boolean -- only this single bit is ever revealed
6. Individual confidence scores, position sizes, and risk thresholds remain permanently encrypted

**What this prevents:** Front-running based on risk parameter analysis. No observer can determine the agent's position sizing strategy, loss tolerance, or when it is approaching its limits.

### Current Status

- **On-chain CPI integration is complete.** The Anchor program calls Encrypt's `execute_graph` (discriminator = 4) and `request_decryption` (discriminator = 11) via raw CPI. The instruction logic, account layouts, and FHE comparison graph are implemented and deployed.
- **E2E Ika binary works end-to-end.** The `e2e-ika` binary successfully runs the full DKG lifecycle on devnet and has created a real dWallet ([`7ruuv1n...M15o`](https://explorer.solana.com/address/7ruuv1nVgmTiNaPXvQtYRf5DQLjtPb8jH9ekcrmSM15o?cluster=devnet)).
- **Encrypt gRPC SDK is pre-alpha with incomplete protobuf generation.** The `e2e-encrypt` binary contains the full integration code (key upload, ciphertext creation, graph execution), but the current SDK release has broken protobuf stubs that prevent compilation. This is a known upstream issue, not a missing integration.
- **The Python backend simulates encryption locally.** Because the gRPC SDK cannot be called yet, the backend generates ciphertext account references locally to exercise the full pipeline flow. The on-chain program treats these references identically to real ciphertext accounts.
- **No design gap exists.** The CPI flow is ready for production use the moment the Encrypt SDK ships stable protobuf generation. Switching from simulated to real encryption requires updating the backend client, not the on-chain program.

## How Ika Is Used

Ika provides dWallets -- distributed signing keys controlled by Solana programs via 2PC-MPC.

1. A dWallet was created on Solana Devnet via Ika's gRPC DKG protocol
2. The dWallet's authority is transferred to the VAPM program's CPI authority PDA
3. When `finalize_trade` determines all risk checks passed, it performs CPI to Ika's `approve_message` (discriminator = 8)
4. This creates a `MessageApproval` PDA on-chain
5. The Ika network observes the approval and produces the signature via 2PC-MPC
6. The resulting signature is used to execute the Jupiter swap

**What this prevents:** Unauthorized trading. No single party holds the private key. The key cannot sign without on-chain program approval, and the program will not approve without passing encrypted risk checks.

---

## Program Instructions

| Instruction | Purpose |
|-------------|---------|
| `initialize_agent` | Create AgentState PDA with encrypted risk limit refs and dWallet ref |
| `submit_trade` | Store encrypted trade parameter refs and trade message hash |
| `execute_risk_check` | CPI to Encrypt `execute_graph` for FHE comparison |
| `finalize_trade` | Read decrypted boolean; CPI to Ika `approve_message` if all checks pass |
| `log_decision` | Store decision hash on-chain (backward compatibility) |
| `mark_executed` | Flag decision as executed after swap completes |

---

## Target Users and Use Cases

- **AI Agent Operators** who need provable, tamper-proof risk controls for autonomous trading systems. VAPM guarantees that risk limits cannot be bypassed even by the operator.

- **Fund Managers** running algorithmic strategies who require strategy privacy. Encrypted parameters prevent information leakage to competitors and front-runners.

- **Institutional DeFi Participants** who need custody solutions where signing authority is cryptographically bound to risk compliance, not just policy compliance.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| ML | XGBoost, SHAP, scikit-learn |
| Backend | Python 3.11, FastAPI |
| Database | PostgreSQL, Redis |
| Blockchain | Anchor (Rust), solana-py, solders |
| Privacy | Encrypt FHE (raw CPI to `execute_graph`, `request_decryption`) |
| Custody | Ika dWallet (raw CPI to `approve_message`) |
| DEX | Jupiter Aggregator |
| Frontend | Next.js, Tailwind CSS |

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker and Docker Compose
- Solana CLI + Anchor CLI
- Rust / Cargo (for e2e binaries)
- Bun or Node.js (for frontend)

### Setup

```bash
# 1. Clone and configure
git clone https://github.com/sarthaksbiswas97/vapm-agent.git
cd vapm-agent
cp .env.example .env

# 2. Start infrastructure
docker-compose up -d postgres redis

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Create Solana wallet and fund it
solana-keygen new -o ~/.config/solana/id.json
solana config set --url devnet
solana airdrop 2

# 5. Build and deploy program (already deployed at 6xDo2r...cMLx)
anchor build --no-idl
solana program deploy target/deploy/vapm_decisions.so

# 6. Create dWallet via Ika (runs full DKG lifecycle on devnet)
cd e2e-ika && cargo run

# 7. Start backend
cd backend && uvicorn main:app --port 8001

# 8. Start frontend
cd frontend && bun run dev
```

The frontend dashboard at **http://localhost:3000** shows:
- Live SOL/USDC price chart
- ML predictions with SHAP explainability
- Real on-chain agent state from devnet (PDAs, trade verdicts, encrypted refs)
- Clickable Solana Explorer links for all on-chain accounts
- End-to-end pipeline visualization (Market Data -> AI -> Encrypt FHE -> Risk Check -> dWallet)

### E2E Integration Binaries

```bash
# Full Ika lifecycle: DKG, authority transfer, presign, sign
cd e2e-ika && cargo run

# Encrypt integration (note: pre-alpha dependency, may require manual resolution)
cd e2e-encrypt && cargo run
```

---

## API Endpoints

```
GET  /health                  System health check
GET  /agent/status            Agent state and PnL
GET  /agent/onchain           Solana identity and on-chain decisions
GET  /agent/live              Live on-chain data from devnet RPC (PDAs, verdicts, explorer links)
GET  /agent/dwallet           dWallet custody status and risk limits
GET  /agent/encrypt           Encrypt FHE status and encrypted decisions
GET  /market/price            Current SOL/USDC price
GET  /market/candles          Historical OHLCV candles
GET  /predict                 ML prediction with SHAP explanations
GET  /predict/model           Model metadata and performance metrics
GET  /trades/position         Current position state
GET  /trades/status           Trade executor status and recent trades
POST /trade/submit-demo       Submit a demo trade through full pipeline
GET  /risk/state              Risk metrics
GET  /risk/limits             Configured risk limits (bps)
GET  /backtest/results        Historical backtest performance
GET  /performance/metrics     Model accuracy, Sharpe ratio, win rate
POST /agent/register          Register agent on-chain
GET  /verify/{decision_id}    Verify decision hash against on-chain record
```

---

## Project Structure

```
vapm-agent/
|-- programs/vapm_decisions/      Anchor program (Rust)
|   |-- src/lib.rs                CPI to Encrypt + Ika, risk checks, trade approval
|   |-- src/encrypt_fns.rs        Encrypt FHE helper functions
|-- e2e-ika/                      Ika dWallet E2E binary (DKG, authority transfer, signing)
|-- e2e-encrypt/                  Encrypt FHE E2E binary
|-- backend/
|   |-- main.py                   FastAPI (30+ endpoints)
|   |-- services/
|   |   |-- onchain_reader.py     Live devnet RPC reader (parses PDAs, verdicts)
|   |   |-- blockchain_client.py  Solana + Jupiter integration
|   |   |-- dwallet_client.py     Ika dWallet management
|   |   |-- encrypt_client.py     Encrypt FHE privacy
|   |   |-- trade_executor.py     Trading orchestrator
|   |   |-- risk_guardian.py      Risk management engine
|   |   |-- prediction_service.py XGBoost + SHAP inference
|   |-- models/                   Pydantic data models
|   |-- core/                     Technical indicators (20+)
|-- ml/                           ML training, backtesting, model comparison
|-- frontend/                     Next.js dashboard (3 pages, live on-chain data)
|   |-- src/app/                  Dashboard, analytics, model pages
|   |-- src/components/           Pipeline, DWalletCard, EncryptCard, charts
|   |-- src/lib/                  API client, types
```

---

## License

MIT License - see [LICENSE](LICENSE) for details.

---

Built for the **Frontier Hackathon** -- Encrypt + Ika track (Hybrid Solutions) on Superteam Earn.
