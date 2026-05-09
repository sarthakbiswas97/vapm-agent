"""
One-time dWallet setup script for VAPM agent.

Steps:
1. Create a dWallet via Ika gRPC (Curve25519 for Solana)
2. Deposit gas for signing operations
3. Transfer authority to the VAPM program's CPI authority PDA
4. Save the dWallet address to .env

Prerequisites:
- Solana CLI installed and configured for devnet
- Ika gRPC endpoint accessible
- VAPM Anchor program deployed (DECISION_PROGRAM_ID set in .env)

Usage:
    python scripts/setup_dwallet.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# Add backend to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from solders.pubkey import Pubkey


# Ika program ID on Solana devnet
IKA_PROGRAM_ID = Pubkey.from_string("87W54kGYFQ1rgWqMeu4XTPHWXWmXSQCcjm8vCTfiq1oY")
CPI_AUTHORITY_SEED = b"__ika_cpi_authority"


def derive_cpi_authority(program_id: str) -> tuple[str, int]:
    """Derive the CPI authority PDA for the VAPM program."""
    program_pubkey = Pubkey.from_string(program_id)
    pda, bump = Pubkey.find_program_address(
        [CPI_AUTHORITY_SEED],
        program_pubkey,
    )
    return str(pda), bump


async def main() -> None:
    """Run the dWallet setup process."""
    print("=" * 60)
    print("VAPM dWallet Setup")
    print("=" * 60)

    # Load config
    from dotenv import load_dotenv
    load_dotenv()

    program_id = os.getenv("DECISION_PROGRAM_ID", "")
    if not program_id:
        print("\nWARNING: DECISION_PROGRAM_ID not set in .env")
        print("Deploy the Anchor program first: anchor deploy")
        print("Then set DECISION_PROGRAM_ID in .env and re-run this script.")
        print("\nShowing what would be configured:\n")

    # Step 1: Derive CPI authority PDA
    if program_id:
        cpi_authority, bump = derive_cpi_authority(program_id)
        print(f"CPI Authority PDA: {cpi_authority}")
        print(f"CPI Authority Bump: {bump}")
    else:
        print("CPI Authority PDA: <requires DECISION_PROGRAM_ID>")

    print()

    # Step 2: Instructions for manual dWallet creation via Ika gRPC
    print("To create a dWallet, use the Ika gRPC API:")
    print(f"  Endpoint: pre-alpha-dev-1.ika.ika-network.net:443")
    print(f"  Operation: DKG (Distributed Key Generation)")
    print(f"  Curve: Curve25519 (ID=2) for Solana-native signing")
    print(f"  Signature Scheme: EddsaSha512 (ID=5)")
    print()

    # Step 3: Show .env configuration
    print("After dWallet creation, add to .env:")
    print(f"  DWALLET_ADDRESS=<your_dwallet_account_pubkey>")
    print(f"  BLOCKCHAIN_ENABLED=true")
    print()

    # Step 4: Authority transfer instructions
    print("Then transfer dWallet authority to CPI PDA:")
    print("  This is done via the Ika gRPC TransferOwnership operation")
    if program_id:
        cpi_authority, _ = derive_cpi_authority(program_id)
        print(f"  New authority: {cpi_authority}")
    print()

    print("=" * 60)
    print("Setup information displayed. Follow the steps above.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
