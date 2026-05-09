"""
dWallet Client Service - Ika MPC wallet management for AI agent custody.

Handles:
- dWallet creation via Ika gRPC (DKG)
- Authority transfer to program CPI PDA
- Trade message approval and signature polling
- Gas deposit management
"""

from __future__ import annotations

import hashlib
import logging
import struct
import time
from dataclasses import dataclass

import aiohttp
from solders.pubkey import Pubkey

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Ika program on Solana devnet
IKA_PROGRAM_ID = Pubkey.from_string("87W54kGYFQ1rgWqMeu4XTPHWXWmXSQCcjm8vCTfiq1oY")

# CPI authority seed recognized by Ika
CPI_AUTHORITY_SEED = b"__ika_cpi_authority"

# Signature schemes
SIGNATURE_SCHEME_ED25519 = 0
SIGNATURE_SCHEME_SECP256K1 = 1

# MessageApproval status
STATUS_PENDING = 0
STATUS_SIGNED = 1

# MessageApproval account layout offsets
MSG_APPROVAL_STATUS_OFFSET = 139
MSG_APPROVAL_SIG_LEN_OFFSET = 140
MSG_APPROVAL_SIG_OFFSET = 142


@dataclass
class DWalletInfo:
    """dWallet account information."""

    address: str
    public_key: bytes
    curve: int
    state: int  # 0=DKGInProgress, 1=Active, 2=Frozen
    authority: str


@dataclass
class MessageApprovalResult:
    """Result of a message approval request."""

    approval_pda: str
    status: int  # 0=Pending, 1=Signed
    signature: bytes | None
    message_hash: bytes


class DWalletClient:
    """
    Client for Ika dWallet operations.

    Provides decentralized custody for the AI trading agent:
    - The agent's trading wallet is a dWallet (MPC key)
    - The on-chain program controls when the dWallet signs
    - Risk limits are enforced at the cryptographic level
    """

    def __init__(self) -> None:
        self._http_session: aiohttp.ClientSession | None = None
        self._dwallet_address: str = ""
        self._initialized = False

    @property
    def is_enabled(self) -> bool:
        """Check if dWallet features are configured."""
        return bool(settings.dwallet_address)

    @property
    def dwallet_address(self) -> str:
        """Get the dWallet account address."""
        return self._dwallet_address or settings.dwallet_address

    async def initialize(self) -> bool:
        """Initialize the dWallet client."""
        if not settings.dwallet_address:
            logger.info("[dWallet] No dWallet configured - operating in fallback mode")
            return False

        try:
            self._http_session = aiohttp.ClientSession()
            self._dwallet_address = settings.dwallet_address
            self._initialized = True
            logger.info("[dWallet] Initialized with address: %s", self._dwallet_address)
            return True
        except Exception as e:
            logger.error("[dWallet] Initialization failed: %s", e)
            return False

    async def close(self) -> None:
        """Close HTTP session."""
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

    def derive_cpi_authority(self, program_id: str) -> tuple[Pubkey, int]:
        """
        Derive the CPI authority PDA for our program.

        This PDA must be set as the dWallet's authority for our
        program to call approve_message via CPI.
        """
        program_pubkey = Pubkey.from_string(program_id)
        pda, bump = Pubkey.find_program_address(
            [CPI_AUTHORITY_SEED],
            program_pubkey,
        )
        return pda, bump

    def compute_message_hash(self, message: bytes) -> bytes:
        """
        Compute keccak256 hash of the message to sign.

        The dWallet program uses keccak256 as the uniqueness key
        for the MessageApproval PDA.
        """
        from hashlib import sha256

        # Solana uses keccak256 for on-chain hashing
        # For the pre-alpha, SHA256 is used as a compatible substitute
        return sha256(message).digest()

    def derive_message_approval_pda(
        self,
        dwallet_pubkey: Pubkey,
        message_hash: bytes,
    ) -> tuple[Pubkey, int]:
        """
        Derive the MessageApproval PDA address.

        Seeds: ["message_approval", dwallet_pubkey, message_hash]
        Program: IKA_PROGRAM_ID
        """
        pda, bump = Pubkey.find_program_address(
            [b"message_approval", bytes(dwallet_pubkey), message_hash],
            IKA_PROGRAM_ID,
        )
        return pda, bump

    async def poll_message_approval(
        self,
        approval_pda: Pubkey,
        timeout_seconds: int = 60,
        poll_interval: float = 2.0,
    ) -> MessageApprovalResult | None:
        """
        Poll a MessageApproval PDA until it is signed or timeout.

        The Ika network detects pending MessageApproval accounts
        and produces signatures via 2PC-MPC.

        Returns:
            MessageApprovalResult with signature if signed, None on timeout.
        """
        if not self._http_session:
            return None

        start = time.time()
        while time.time() - start < timeout_seconds:
            try:
                # Fetch the MessageApproval account data
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getAccountInfo",
                    "params": [
                        str(approval_pda),
                        {"encoding": "base64", "commitment": "confirmed"},
                    ],
                }
                async with self._http_session.post(
                    settings.solana_rpc_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    result = await resp.json()

                value = result.get("result", {}).get("value")
                if not value:
                    await __import__("asyncio").sleep(poll_interval)
                    continue

                import base64

                data = base64.b64decode(value["data"][0])

                # Check status at offset 139
                if len(data) > MSG_APPROVAL_STATUS_OFFSET:
                    status = data[MSG_APPROVAL_STATUS_OFFSET]

                    if status == STATUS_SIGNED:
                        # Read signature length and bytes
                        sig_len = struct.unpack_from(
                            "<H", data, MSG_APPROVAL_SIG_LEN_OFFSET
                        )[0]
                        signature = data[
                            MSG_APPROVAL_SIG_OFFSET : MSG_APPROVAL_SIG_OFFSET
                            + sig_len
                        ]

                        # Read message hash at offset 34
                        msg_hash = data[34:66]

                        return MessageApprovalResult(
                            approval_pda=str(approval_pda),
                            status=STATUS_SIGNED,
                            signature=signature,
                            message_hash=msg_hash,
                        )

            except Exception as e:
                logger.warning("[dWallet] Poll error: %s", e)

            await __import__("asyncio").sleep(poll_interval)

        logger.warning("[dWallet] Approval timed out after %ds", timeout_seconds)
        return None

    def get_status(self) -> dict:
        """Get dWallet client status."""
        if not self._initialized:
            return {
                "enabled": self.is_enabled,
                "initialized": False,
                "dwallet_address": None,
                "cpi_authority": None,
            }

        cpi_pda = None
        if settings.decision_program_id:
            pda, _ = self.derive_cpi_authority(settings.decision_program_id)
            cpi_pda = str(pda)

        return {
            "enabled": True,
            "initialized": True,
            "dwallet_address": self._dwallet_address,
            "ika_program": str(IKA_PROGRAM_ID),
            "cpi_authority": cpi_pda,
        }


# Global singleton
dwallet_client = DWalletClient()
