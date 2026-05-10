use anchor_lang::prelude::*;
use anchor_lang::solana_program::{
    instruction::{AccountMeta, Instruction},
    program::invoke_signed,
    keccak,
};

declare_id!("6xDo2r8Edvu1MHxwUtqmmzm3Auavf2fokbjGoJHxcMLx");

// Ika dWallet program on devnet
const IKA_PROGRAM: Pubkey = pubkey!("87W54kGYFQ1rgWqMeu4XTPHWXWmXSQCcjm8vCTfiq1oY");
const IKA_CPI_SEED: &[u8] = b"__ika_cpi_authority";
const IX_APPROVE_MESSAGE: u8 = 8;

// Encrypt program on devnet
const ENCRYPT_PROGRAM: Pubkey = pubkey!("4ebfzWdKnrnGseuQpezXdG8yCdHqwQ1SSBHD3bWArND8");
const ENCRYPT_CPI_SEED: &[u8] = b"__encrypt_cpi_authority";
const IX_EXECUTE_GRAPH: u8 = 4;
const IX_REQUEST_DECRYPTION: u8 = 11;
const IX_CREATE_PLAINTEXT: u8 = 2;

#[program]
pub mod vapm_decisions {
    use super::*;

    /// Register agent with encrypted risk limits and dWallet reference.
    /// Risk limits are stored as ciphertext account references (Pubkeys),
    /// NOT plaintext values. Nobody can read the limits from on-chain data.
    pub fn initialize_agent(
        ctx: Context<InitializeAgent>,
        name: String,
        dwallet: Pubkey,
        enc_max_position: Pubkey,
        enc_max_daily_loss: Pubkey,
        enc_max_drawdown: Pubkey,
    ) -> Result<()> {
        require!(name.len() <= 32, VapmError::InvalidInput);
        let a = &mut ctx.accounts.agent_state;
        a.authority = ctx.accounts.authority.key();
        a.name = name;
        a.decision_count = 0;
        a.dwallet = dwallet;
        a.enc_max_position = enc_max_position;
        a.enc_max_daily_loss = enc_max_daily_loss;
        a.enc_max_drawdown = enc_max_drawdown;
        a.trades_approved = 0;
        a.trades_rejected = 0;
        a.bump = ctx.bumps.agent_state;
        Ok(())
    }

    /// AI agent submits a trade proposal with encrypted parameters.
    /// Parameters are ciphertext account references created via Encrypt gRPC.
    pub fn submit_trade(
        ctx: Context<SubmitTrade>,
        enc_position: Pubkey,
        enc_pnl: Pubkey,
        enc_drawdown: Pubkey,
        message_hash: [u8; 32],
    ) -> Result<()> {
        let a = &mut ctx.accounts.agent_state;
        let t = &mut ctx.accounts.trade_proposal;
        t.agent = a.key();
        t.proposer = ctx.accounts.authority.key();
        t.index = a.decision_count;
        t.enc_position = enc_position;
        t.enc_pnl = enc_pnl;
        t.enc_drawdown = enc_drawdown;
        t.fhe_pos_ok = Pubkey::default();
        t.fhe_pnl_ok = Pubkey::default();
        t.fhe_dd_ok = Pubkey::default();
        t.verdict = 0; // Pending
        t.message_hash = message_hash;
        t.timestamp = Clock::get()?.unix_timestamp;
        t.bump = ctx.bumps.trade_proposal;
        a.decision_count += 1;
        Ok(())
    }

    /// Execute FHE risk check: compares encrypted trade param <= encrypted limit.
    /// CPI to Encrypt program's execute_graph instruction.
    /// Must be called 3 times (position, pnl, drawdown).
    ///
    /// remaining_accounts layout (12 accounts):
    /// [0] encrypt_program, [1] config, [2] deposit(w), [3] network_key,
    /// [4] payer(w,s), [5] event_authority, [6] encrypt_cpi_authority,
    /// [7] caller_program, [8] input_ct_a, [9] input_ct_b, [10] output_ct(w),
    /// [11] system_program
    pub fn execute_risk_check(
        ctx: Context<ExecuteRiskCheck>,
        check_type: u8,
        encrypt_cpi_bump: u8,
        graph_data: Vec<u8>,
    ) -> Result<()> {
        let a = &ctx.accounts.agent_state;
        let t = &mut ctx.accounts.trade_proposal;
        let rem = &ctx.remaining_accounts;
        require!(rem.len() >= 11, VapmError::InvalidInput);

        // Select input ciphertexts based on check_type
        let (trade_ct, limit_ct) = match check_type {
            0 => (t.enc_position, a.enc_max_position),   // position check
            1 => (t.enc_pnl, a.enc_max_daily_loss),       // daily loss check
            2 => (t.enc_drawdown, a.enc_max_drawdown),     // drawdown check
            _ => return Err(VapmError::InvalidInput.into()),
        };

        // Verify the correct ciphertext accounts are passed
        require!(rem[8].key() == trade_ct, VapmError::InvalidInput);
        require!(rem[9].key() == limit_ct, VapmError::InvalidInput);

        // Build execute_graph instruction (disc=4)
        let mut ix_data = vec![IX_EXECUTE_GRAPH];
        ix_data.extend_from_slice(&(graph_data.len() as u16).to_le_bytes());
        ix_data.extend_from_slice(&graph_data);
        ix_data.extend_from_slice(&2u16.to_le_bytes()); // num_inputs = 2

        let ix = Instruction {
            program_id: ENCRYPT_PROGRAM,
            accounts: vec![
                AccountMeta::new(rem[1].key(), false),        // config
                AccountMeta::new(rem[2].key(), false),        // deposit
                AccountMeta::new_readonly(rem[7].key(), false), // caller_program
                AccountMeta::new_readonly(rem[6].key(), true),  // cpi_authority (signer)
                AccountMeta::new_readonly(rem[3].key(), false), // network_key
                AccountMeta::new(rem[4].key(), true),          // payer
                AccountMeta::new_readonly(rem[5].key(), false), // event_authority
                AccountMeta::new_readonly(rem[0].key(), false), // encrypt_program
                AccountMeta::new_readonly(rem[8].key(), false), // input_ct_a
                AccountMeta::new_readonly(rem[9].key(), false), // input_ct_b
                AccountMeta::new(rem[10].key(), false),         // output_ct
            ],
            data: ix_data,
        };

        let seeds = &[ENCRYPT_CPI_SEED, &[encrypt_cpi_bump]];
        invoke_signed(
            &ix,
            &rem.iter().map(|a| a.to_account_info()).collect::<Vec<_>>(),
            &[seeds],
        )?;

        // Store FHE result ciphertext reference
        match check_type {
            0 => t.fhe_pos_ok = rem[10].key(),
            1 => t.fhe_pnl_ok = rem[10].key(),
            2 => t.fhe_dd_ok = rem[10].key(),
            _ => {}
        }

        Ok(())
    }

    /// Finalize trade: if risk checks passed, CPI to Ika approve_message.
    /// This is the key instruction -- it gates dWallet signing on FHE results.
    ///
    /// For the hackathon demo, this accepts a `risk_passed` boolean parameter
    /// that would come from the decrypted FHE result in production.
    /// In production, this reads from Encrypt's DecryptionRequest account.
    ///
    /// remaining_accounts layout for Ika CPI (7 accounts):
    /// [0] coordinator, [1] message_approval(w), [2] dwallet,
    /// [3] caller_program, [4] ika_cpi_authority, [5] payer(w,s),
    /// [6] system_program, [7] ika_program
    pub fn finalize_trade(
        ctx: Context<FinalizeTrade>,
        risk_passed: bool,
        ika_cpi_bump: u8,
        user_pubkey: [u8; 32],
        signature_scheme: u16,
        msg_approval_bump: u8,
    ) -> Result<()> {
        let a = &mut ctx.accounts.agent_state;
        let t = &mut ctx.accounts.trade_proposal;

        if !risk_passed {
            t.verdict = 2; // Rejected
            a.trades_rejected += 1;
            emit!(TradeEvent {
                agent: a.key(),
                approved: false,
                message_hash: t.message_hash,
            });
            return Ok(());
        }

        // Risk check passed -- CPI to Ika approve_message
        let rem = &ctx.remaining_accounts;
        require!(rem.len() >= 8, VapmError::InvalidInput);

        // Build approve_message instruction (disc=8, 100 bytes)
        let message_digest = keccak::hash(&t.message_hash).0;
        let metadata_digest = [0u8; 32]; // no metadata

        let mut ix_data = Vec::with_capacity(100);
        ix_data.push(IX_APPROVE_MESSAGE);
        ix_data.push(msg_approval_bump);
        ix_data.extend_from_slice(&message_digest);
        ix_data.extend_from_slice(&metadata_digest);
        ix_data.extend_from_slice(&user_pubkey);
        ix_data.extend_from_slice(&signature_scheme.to_le_bytes());

        let ix = Instruction {
            program_id: IKA_PROGRAM,
            accounts: vec![
                AccountMeta::new_readonly(rem[0].key(), false), // coordinator
                AccountMeta::new(rem[1].key(), false),          // message_approval
                AccountMeta::new_readonly(rem[2].key(), false), // dwallet
                AccountMeta::new_readonly(rem[3].key(), false), // caller_program
                AccountMeta::new_readonly(rem[4].key(), true),  // cpi_authority (signer)
                AccountMeta::new(rem[5].key(), true),           // payer
                AccountMeta::new_readonly(rem[6].key(), false), // system_program
            ],
            data: ix_data,
        };

        let seeds = &[IKA_CPI_SEED, &[ika_cpi_bump]];
        invoke_signed(
            &ix,
            &[
                rem[0].to_account_info(), rem[1].to_account_info(),
                rem[2].to_account_info(), rem[3].to_account_info(),
                rem[4].to_account_info(), rem[5].to_account_info(),
                rem[6].to_account_info(), rem[7].to_account_info(),
            ],
            &[seeds],
        )?;

        t.verdict = 1; // Approved
        a.trades_approved += 1;

        emit!(TradeEvent {
            agent: a.key(),
            approved: true,
            message_hash: t.message_hash,
        });

        Ok(())
    }

    /// Log a decision hash on-chain (backward compat).
    pub fn log_decision(
        ctx: Context<LogDecision>,
        decision_hash: [u8; 32],
        confidence: u16,
        risk_score: u16,
    ) -> Result<()> {
        let a = &mut ctx.accounts.agent_state;
        let r = &mut ctx.accounts.decision_record;
        r.agent = ctx.accounts.authority.key();
        r.decision_hash = decision_hash;
        r.confidence = confidence;
        r.risk_score = risk_score;
        r.timestamp = Clock::get()?.unix_timestamp;
        r.executed = false;
        r.bump = ctx.bumps.decision_record;
        a.decision_count += 1;
        Ok(())
    }

    /// Mark decision as executed.
    pub fn mark_executed(ctx: Context<MarkExecuted>, _idx: u32) -> Result<()> {
        let r = &mut ctx.accounts.decision_record;
        require!(!r.executed, VapmError::AlreadyExecuted);
        r.executed = true;
        Ok(())
    }
}

// ── Accounts ────────────────────────────────────────────────

#[derive(Accounts)]
pub struct InitializeAgent<'info> {
    #[account(init, payer = authority, space = AgentState::SIZE,
              seeds = [b"agent", authority.key().as_ref()], bump)]
    pub agent_state: Account<'info, AgentState>,
    #[account(mut)]
    pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct SubmitTrade<'info> {
    #[account(mut, seeds = [b"agent", authority.key().as_ref()],
              bump = agent_state.bump, has_one = authority)]
    pub agent_state: Account<'info, AgentState>,
    #[account(init, payer = authority, space = TradeProposal::SIZE,
              seeds = [b"t", agent_state.key().as_ref(),
                       &agent_state.decision_count.to_le_bytes()], bump)]
    pub trade_proposal: Account<'info, TradeProposal>,
    #[account(mut)]
    pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct ExecuteRiskCheck<'info> {
    #[account(seeds = [b"agent", authority.key().as_ref()],
              bump = agent_state.bump, has_one = authority)]
    pub agent_state: Account<'info, AgentState>,
    #[account(mut, constraint = trade_proposal.agent == agent_state.key() @ VapmError::InvalidInput)]
    pub trade_proposal: Account<'info, TradeProposal>,
    pub authority: Signer<'info>,
    // remaining_accounts: Encrypt CPI accounts (11+)
}

#[derive(Accounts)]
pub struct FinalizeTrade<'info> {
    #[account(mut, seeds = [b"agent", authority.key().as_ref()],
              bump = agent_state.bump, has_one = authority)]
    pub agent_state: Account<'info, AgentState>,
    #[account(mut, constraint = trade_proposal.agent == agent_state.key() @ VapmError::InvalidInput)]
    pub trade_proposal: Account<'info, TradeProposal>,
    #[account(mut)]
    pub authority: Signer<'info>,
    // remaining_accounts: Ika CPI accounts (8)
}

#[derive(Accounts)]
pub struct LogDecision<'info> {
    #[account(mut, seeds = [b"agent", authority.key().as_ref()],
              bump = agent_state.bump, has_one = authority)]
    pub agent_state: Account<'info, AgentState>,
    #[account(init, payer = authority, space = DecisionRecord::SIZE,
              seeds = [b"d", authority.key().as_ref(),
                       &agent_state.decision_count.to_le_bytes()], bump)]
    pub decision_record: Account<'info, DecisionRecord>,
    #[account(mut)]
    pub authority: Signer<'info>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
#[instruction(_idx: u32)]
pub struct MarkExecuted<'info> {
    #[account(seeds = [b"agent", authority.key().as_ref()],
              bump = agent_state.bump, has_one = authority)]
    pub agent_state: Account<'info, AgentState>,
    #[account(mut, seeds = [b"d", authority.key().as_ref(),
                            &(_idx as u64).to_le_bytes()],
              bump = decision_record.bump)]
    pub decision_record: Account<'info, DecisionRecord>,
    pub authority: Signer<'info>,
}

// ── State ───────────────────────────────────────────────────

#[account]
pub struct AgentState {
    pub authority: Pubkey,           // 32
    pub name: String,                // 4+32
    pub decision_count: u64,         // 8
    pub dwallet: Pubkey,             // 32 - dWallet PDA on Ika
    // Encrypted risk limits (ciphertext account refs)
    pub enc_max_position: Pubkey,    // 32 - EUint64 ciphertext
    pub enc_max_daily_loss: Pubkey,  // 32 - EUint64 ciphertext
    pub enc_max_drawdown: Pubkey,    // 32 - EUint64 ciphertext
    pub trades_approved: u32,        // 4
    pub trades_rejected: u32,        // 4
    pub bump: u8,                    // 1
}

impl AgentState {
    pub const SIZE: usize = 8 + 32 + 36 + 8 + 32 + 32 + 32 + 32 + 4 + 4 + 1 + 16;
}

#[account]
pub struct TradeProposal {
    pub agent: Pubkey,               // 32 - agent_state key
    pub proposer: Pubkey,            // 32
    pub index: u64,                  // 8
    // Encrypted trade parameters
    pub enc_position: Pubkey,        // 32 - EUint64
    pub enc_pnl: Pubkey,             // 32 - EUint64
    pub enc_drawdown: Pubkey,        // 32 - EUint64
    // FHE comparison results
    pub fhe_pos_ok: Pubkey,          // 32 - EBool from execute_graph
    pub fhe_pnl_ok: Pubkey,          // 32 - EBool
    pub fhe_dd_ok: Pubkey,           // 32 - EBool
    // Verdict
    pub verdict: u8,                 // 1 - 0=Pending, 1=Approved, 2=Rejected
    pub message_hash: [u8; 32],      // 32
    pub timestamp: i64,              // 8
    pub bump: u8,                    // 1
}

impl TradeProposal {
    pub const SIZE: usize = 8 + 32*9 + 8 + 1 + 32 + 8 + 1 + 16;
}

#[account]
pub struct DecisionRecord {
    pub agent: Pubkey,               // 32
    pub decision_hash: [u8; 32],     // 32
    pub confidence: u16,             // 2
    pub risk_score: u16,             // 2
    pub timestamp: i64,              // 8
    pub executed: bool,              // 1
    pub bump: u8,                    // 1
}

impl DecisionRecord {
    pub const SIZE: usize = 8 + 32 + 32 + 2 + 2 + 8 + 1 + 1 + 8;
}

// ── Events ──────────────────────────────────────────────────

#[event]
pub struct TradeEvent {
    pub agent: Pubkey,
    pub approved: bool,
    pub message_hash: [u8; 32],
}

// ── Errors ──────────────────────────────────────────────────

#[error_code]
pub enum VapmError {
    #[msg("Invalid input")]
    InvalidInput,
    #[msg("Already executed")]
    AlreadyExecuted,
}
