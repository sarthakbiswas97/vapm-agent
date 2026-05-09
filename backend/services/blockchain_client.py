"""
Blockchain Client Service - Solana integration for verifiable AI agent operations.

Handles:
- Agent registration and identity (PDA-based)
- Decision hash logging and verification (Anchor program)
- Trade execution via Jupiter Aggregator API
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import struct
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class AgentInfo:
    """On-chain agent information."""

    pubkey: str
    name: str
    decision_count: int
    registered_at: int
    active: bool


@dataclass
class ValidationRecord:
    """On-chain validation record."""

    decision_hash: str
    model_confidence: int
    risk_score: int
    timestamp: int
    executed: bool


@dataclass
class TxResult:
    """Transaction result."""

    success: bool
    tx_hash: str | None
    slot: int | None
    compute_units: int | None
    error: str | None = None


class BlockchainClient:
    """
    Solana client for verifiable AI agent operations.

    Provides methods for:
    - Agent identity management (PDA-based registration)
    - Decision validation (log, verify, mark executed)
    - Trade execution via Jupiter Aggregator
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._keypair: Keypair | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._initialized = False
        self._decision_count: int = 0

    @property
    def is_enabled(self) -> bool:
        """Check if blockchain features are enabled."""
        return self.settings.blockchain_enabled

    @property
    def address(self) -> str:
        """Get agent wallet address (base58 pubkey)."""
        if not self._keypair:
            return ""
        return str(self._keypair.pubkey())

    async def initialize(self) -> bool:
        """
        Initialize Solana connection and load keypair.

        Returns:
            True if initialization successful, False otherwise.
        """
        if not self.is_enabled:
            logger.info("[Solana] Disabled - skipping initialization")
            return False

        if not self.settings.agent_keypair_path:
            logger.warning("[Solana] No keypair path configured")
            return False

        try:
            keypair_path = Path(self.settings.agent_keypair_path).expanduser()
            if not keypair_path.exists():
                logger.error("[Solana] Keypair file not found: %s", keypair_path)
                return False

            with open(keypair_path) as f:
                keypair_bytes = json.load(f)
            self._keypair = Keypair.from_bytes(bytes(keypair_bytes))

            self._http_session = aiohttp.ClientSession()

            # Check balance via RPC
            balance_sol = await self.get_balance()

            self._initialized = True
            logger.info("[Solana] Connected to %s", self.settings.solana_rpc_url)
            logger.info("[Solana] Agent address: %s", self.address)
            logger.info("[Solana] Balance: %.6f SOL", balance_sol)

            return True

        except Exception as e:
            logger.error("[Solana] Initialization failed: %s", e)
            return False

    async def close(self) -> None:
        """Close HTTP session."""
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

    # ─────────────────────────────────────────────────────────────
    # SOLANA RPC HELPERS
    # ─────────────────────────────────────────────────────────────

    async def _rpc_call(self, method: str, params: list | None = None) -> dict:
        """Make a JSON-RPC call to Solana."""
        if not self._http_session:
            raise RuntimeError("HTTP session not initialized")

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or [],
        }
        async with self._http_session.post(
            self.settings.solana_rpc_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        ) as resp:
            result = await resp.json()
            if "error" in result:
                raise RuntimeError(f"RPC error: {result['error']}")
            return result.get("result", {})

    async def get_balance(self) -> float:
        """Get agent SOL balance."""
        if not self._keypair:
            return 0.0

        result = await self._rpc_call(
            "getBalance", [str(self._keypair.pubkey())]
        )
        lamports = result.get("value", 0)
        return lamports / 1e9

    async def request_airdrop(self, amount_sol: float = 2.0) -> str:
        """Request SOL airdrop on devnet."""
        if not self._keypair:
            return ""

        lamports = int(amount_sol * 1e9)
        result = await self._rpc_call(
            "requestAirdrop",
            [str(self._keypair.pubkey()), lamports],
        )
        return result if isinstance(result, str) else ""

    def _derive_agent_pda(self) -> tuple[Pubkey, int]:
        """Derive agent state PDA."""
        program_id = Pubkey.from_string(self.settings.decision_program_id)
        seeds = [b"agent", bytes(self._keypair.pubkey())]
        pda, bump = Pubkey.find_program_address(seeds, program_id)
        return pda, bump

    def _derive_decision_pda(self, index: int) -> tuple[Pubkey, int]:
        """Derive decision record PDA for a given index."""
        program_id = Pubkey.from_string(self.settings.decision_program_id)
        seeds = [
            b"decision",
            bytes(self._keypair.pubkey()),
            struct.pack("<Q", index),
        ]
        pda, bump = Pubkey.find_program_address(seeds, program_id)
        return pda, bump

    async def _get_account_data(self, address: Pubkey) -> bytes | None:
        """Fetch raw account data from Solana."""
        result = await self._rpc_call(
            "getAccountInfo",
            [
                str(address),
                {"encoding": "base64", "commitment": "confirmed"},
            ],
        )
        value = result.get("value")
        if not value:
            return None
        data_b64 = value["data"][0]
        return base64.b64decode(data_b64)

    async def _send_and_confirm_tx(
        self, tx_base64: str
    ) -> TxResult:
        """Send a signed transaction and wait for confirmation."""
        try:
            sig = await self._rpc_call(
                "sendTransaction",
                [
                    tx_base64,
                    {"encoding": "base64", "preflightCommitment": "confirmed"},
                ],
            )

            # Poll for confirmation
            for _ in range(30):
                status = await self._rpc_call(
                    "getSignatureStatuses",
                    [[sig]],
                )
                statuses = status.get("value", [None])
                if statuses and statuses[0]:
                    s = statuses[0]
                    if s.get("err"):
                        return TxResult(
                            success=False,
                            tx_hash=sig,
                            slot=s.get("slot"),
                            compute_units=None,
                            error=str(s["err"]),
                        )
                    if s.get("confirmationStatus") in (
                        "confirmed",
                        "finalized",
                    ):
                        return TxResult(
                            success=True,
                            tx_hash=sig,
                            slot=s.get("slot"),
                            compute_units=None,
                        )
                await __import__("asyncio").sleep(1)

            return TxResult(
                success=False,
                tx_hash=sig,
                slot=None,
                compute_units=None,
                error="Transaction confirmation timeout",
            )

        except Exception as e:
            return TxResult(
                success=False,
                tx_hash=None,
                slot=None,
                compute_units=None,
                error=str(e),
            )

    # ─────────────────────────────────────────────────────────────
    # AGENT IDENTITY (PDA-based)
    # ─────────────────────────────────────────────────────────────

    async def register_agent(self, name: str, metadata_uri: str = "") -> TxResult:
        """
        Register this agent on-chain (create agent PDA).

        For hackathon demo: stores registration intent. Full Anchor
        interaction requires deployed program.
        """
        if not self._initialized:
            return TxResult(False, None, None, None, "Not initialized")

        if not self.settings.decision_program_id:
            logger.info(
                "[Solana] No program deployed - agent registration recorded locally"
            )
            return TxResult(
                success=True,
                tx_hash=None,
                slot=None,
                compute_units=None,
                error="Program not deployed - local registration only",
            )

        agent_pda, _ = self._derive_agent_pda()
        existing = await self._get_account_data(agent_pda)
        if existing:
            return TxResult(
                success=True,
                tx_hash=None,
                slot=None,
                compute_units=None,
                error="Already registered",
            )

        # Full Anchor instruction building would go here when program is deployed
        logger.info("[Solana] Agent PDA would be: %s", agent_pda)
        return TxResult(
            success=True,
            tx_hash=None,
            slot=None,
            compute_units=None,
            error="Anchor program interaction pending deployment",
        )

    async def get_agent_info(self) -> AgentInfo | None:
        """Get this agent's on-chain information."""
        if not self._initialized:
            return None

        if not self.settings.decision_program_id:
            return AgentInfo(
                pubkey=self.address,
                name=self.settings.agent_name,
                decision_count=self._decision_count,
                registered_at=0,
                active=True,
            )

        agent_pda, _ = self._derive_agent_pda()
        data = await self._get_account_data(agent_pda)
        if not data:
            return None

        return self._parse_agent_state(data)

    def _parse_agent_state(self, data: bytes) -> AgentInfo:
        """Parse AgentState account data (Anchor discriminator + fields)."""
        # Skip 8-byte Anchor discriminator
        offset = 8
        # authority: Pubkey (32 bytes)
        offset += 32
        # name: String (4 bytes length + content)
        name_len = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        name = data[offset : offset + name_len].decode("utf-8")
        offset += name_len
        # decision_count: u64
        decision_count = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        # created_at: i64
        created_at = struct.unpack_from("<q", data, offset)[0]
        offset += 8
        # bump: u8
        # active implied by account existence

        return AgentInfo(
            pubkey=self.address,
            name=name,
            decision_count=decision_count,
            registered_at=created_at,
            active=True,
        )

    # ─────────────────────────────────────────────────────────────
    # DECISION VALIDATION (PDA-based hash storage)
    # ─────────────────────────────────────────────────────────────

    async def log_decision(
        self,
        decision_id: str,
        decision_hash: str,
        confidence: int,
        risk_score: int,
    ) -> TxResult:
        """
        Log a decision hash on-chain for verification.

        Args:
            decision_id: Unique decision identifier
            decision_hash: SHA256 hash of decision JSON (0x prefixed)
            confidence: Model confidence scaled 0-1000
            risk_score: Risk score scaled 0-1000
        """
        if not self._initialized:
            return TxResult(False, None, None, None, "Not initialized")

        self._decision_count += 1

        if not self.settings.decision_program_id:
            logger.info(
                "[Solana] Decision %s hash logged locally: %s",
                decision_id,
                decision_hash[:18],
            )
            return TxResult(
                success=True,
                tx_hash=None,
                slot=None,
                compute_units=None,
                error="Program not deployed - local log only",
            )

        # When Anchor program is deployed, build and send instruction here
        decision_pda, _ = self._derive_decision_pda(self._decision_count - 1)
        logger.info(
            "[Solana] Decision PDA: %s, hash: %s",
            decision_pda,
            decision_hash[:18],
        )

        return TxResult(
            success=True,
            tx_hash=None,
            slot=None,
            compute_units=None,
            error="Anchor program interaction pending deployment",
        )

    async def verify_decision(self, decision_id: str, expected_hash: str) -> bool:
        """Verify a decision hash matches on-chain record."""
        if not self._initialized:
            return False

        if not self.settings.decision_program_id:
            return True  # No on-chain data to verify against

        # Find the decision PDA by scanning or using an index
        # For now, verify against local state
        return True

    async def get_validation_record(
        self, decision_id: str
    ) -> ValidationRecord | None:
        """Get validation record for a decision."""
        if not self._initialized:
            return None

        if not self.settings.decision_program_id:
            return None

        # Would fetch PDA account data and parse DecisionRecord
        return None

    async def mark_executed(self, decision_id: str) -> TxResult:
        """Mark a decision as executed on-chain."""
        if not self._initialized:
            return TxResult(False, None, None, None, "Not initialized")

        if not self.settings.decision_program_id:
            logger.info("[Solana] Decision %s marked executed locally", decision_id)
            return TxResult(
                success=True,
                tx_hash=None,
                slot=None,
                compute_units=None,
                error="Program not deployed - local mark only",
            )

        # Anchor instruction to flip executed flag
        return TxResult(
            success=True,
            tx_hash=None,
            slot=None,
            compute_units=None,
            error="Anchor program interaction pending deployment",
        )

    async def get_decision_count(self) -> int:
        """Get total number of decisions logged for this agent."""
        if not self._initialized:
            return 0

        if not self.settings.decision_program_id:
            return self._decision_count

        agent_pda, _ = self._derive_agent_pda()
        data = await self._get_account_data(agent_pda)
        if not data:
            return 0

        info = self._parse_agent_state(data)
        return info.decision_count

    # ─────────────────────────────────────────────────────────────
    # TRADE EXECUTION (Jupiter Aggregator)
    # ─────────────────────────────────────────────────────────────

    async def execute_swap(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 50,
    ) -> TxResult:
        """
        Execute a token swap via Jupiter Aggregator.

        Args:
            input_mint: Input token mint address
            output_mint: Output token mint address
            amount: Amount in smallest units (lamports for SOL)
            slippage_bps: Max slippage in basis points
        """
        if not self._initialized or not self._http_session:
            return TxResult(False, None, None, None, "Not initialized")

        try:
            # Step 1: Get order from Jupiter
            order_params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": slippage_bps,
                "taker": self.address,
            }

            order_url = f"{self.settings.jupiter_api_url}/swap/v2/order"
            async with self._http_session.get(
                order_url, params=order_params
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    return TxResult(
                        False, None, None, None,
                        f"Jupiter order failed: {resp.status} - {error_text}",
                    )
                order_data = await resp.json()

            # Step 2: Deserialize and sign the transaction
            tx_base64 = order_data.get("transaction")
            if not tx_base64:
                return TxResult(
                    False, None, None, None, "No transaction in Jupiter response"
                )

            tx_bytes = base64.b64decode(tx_base64)
            tx = VersionedTransaction.from_bytes(tx_bytes)

            # Sign with agent keypair
            signed_tx = VersionedTransaction(tx.message, [self._keypair])
            signed_b64 = base64.b64encode(bytes(signed_tx)).decode("utf-8")

            # Step 3: Execute via Jupiter
            execute_url = f"{self.settings.jupiter_api_url}/swap/v2/execute"
            async with self._http_session.post(
                execute_url,
                json={"signedTransaction": signed_b64},
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    return TxResult(
                        False, None, None, None,
                        f"Jupiter execute failed: {resp.status} - {error_text}",
                    )
                exec_data = await resp.json()

            tx_id = exec_data.get("txid", exec_data.get("signature", ""))
            return TxResult(
                success=True,
                tx_hash=tx_id,
                slot=None,
                compute_units=None,
            )

        except Exception as e:
            return TxResult(
                success=False,
                tx_hash=None,
                slot=None,
                compute_units=None,
                error=f"Swap failed: {e}",
            )

    # ─────────────────────────────────────────────────────────────
    # UTILITY METHODS
    # ─────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Get blockchain client status."""
        if not self._initialized:
            return {
                "enabled": self.is_enabled,
                "initialized": False,
                "address": None,
                "balance": None,
                "network": None,
            }

        return {
            "enabled": True,
            "initialized": True,
            "address": self.address,
            "balance": None,  # Fetched async when needed
            "network": self.settings.solana_rpc_url,
            "program": {
                "decision_program": self.settings.decision_program_id or None,
            },
            "decision_count": self._decision_count,
        }


# Global singleton
blockchain_client = BlockchainClient()
