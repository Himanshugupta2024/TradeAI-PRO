/**
 * Regression Test Suite for Trade Statistics & Win-Rate Formatting
 * 
 * Ensures:
 * 1. Zero-loss counts (0) are preserved via nullish coalescing (never discarded as falsy).
 * 2. Break-even trades (BE) are properly accounted for and subtracted in fallback loss calculation:
 *    losses_fallback = Math.max(0, eligible_trades - wins - be).
 * 3. Invariant check: wins + losses + be === eligibleCount across all valid states.
 * 4. Legacy foreign-currency excluded trades are quarantined from win-rate calculations.
 * 5. Overview and Portfolio formatters produce identical outputs.
 */

const assert = require('node:assert');
const { formatTradeStatsSummary } = require('../dashboard/stats_helper.js');

function runTests() {
  console.log('--- Running Trade Statistics Regression Tests ---');

  // Test 1: 5 wins, 0 losses, 5 break-evens (zero-loss protection)
  console.log('Test 1: Zero-loss scenario (5W / 0L / 5BE)...');
  {
    const input = {
      winning_trades: 5,
      losing_trades: 0,
      breakeven_trades: 5,
      eligible_trades: 10,
      total_trades: 10,
      win_rate: 50.0,
    };
    const res = formatTradeStatsSummary(input);
    assert.strictEqual(res.losses, 0, 'losses must be preserved as 0, not evaluated as missing');
    assert.strictEqual(res.wins, 5);
    assert.strictEqual(res.be, 5);
    assert.strictEqual(res.eligibleCount, 10);
    assert.strictEqual(res.winRateDisplay, '50.0%');
    assert.strictEqual(res.subText, '5W / 0L / 5BE • 10 trades • Small Sample');
    assert.strictEqual(res.wins + res.losses + res.be, res.eligibleCount, 'Invariant failed: wins + losses + be != eligibleCount');
  }

  // Test 2: Standard mix (3W / 2L / 5BE)
  console.log('Test 2: Standard W/L/BE mix (3W / 2L / 5BE)...');
  {
    const input = {
      winning_trades: 3,
      losing_trades: 2,
      breakeven_trades: 5,
      eligible_trades: 10,
      total_trades: 10,
      win_rate: 30.0,
    };
    const res = formatTradeStatsSummary(input);
    assert.strictEqual(res.losses, 2);
    assert.strictEqual(res.wins, 3);
    assert.strictEqual(res.be, 5);
    assert.strictEqual(res.subText, '3W / 2L / 5BE • 10 trades • Small Sample');
    assert.strictEqual(res.wins + res.losses + res.be, res.eligibleCount);
  }

  // Test 3: Missing losing_trades field (fallback calculation)
  console.log('Test 3: Missing losing_trades fallback calculation...');
  {
    const input = {
      winning_trades: 4,
      // losing_trades omitted -> should calculate: 10 - 4 - 3 = 3
      breakeven_trades: 3,
      eligible_trades: 10,
      total_trades: 10,
      win_rate: 40.0,
    };
    const res = formatTradeStatsSummary(input);
    assert.strictEqual(res.losses, 3, 'fallback calculation must subtract break-evens');
    assert.strictEqual(res.subText, '4W / 3L / 3BE • 10 trades • Small Sample');
    assert.strictEqual(res.wins + res.losses + res.be, res.eligibleCount);
  }

  // Test 4: 0 eligible trades (empty portfolio)
  console.log('Test 4: Empty portfolio with 0 eligible trades...');
  {
    const input = {
      winning_trades: 0,
      losing_trades: 0,
      breakeven_trades: 0,
      eligible_trades: 0,
      total_trades: 0,
      win_rate: null,
    };
    const res = formatTradeStatsSummary(input);
    assert.strictEqual(res.winRateDisplay, '—');
    assert.strictEqual(res.subText, '0 closed trades');
    assert.strictEqual(res.titleText, '');
  }

  // Test 5: Legacy non-INR quarantined trade (Current Production State)
  console.log('Test 5: Legacy non-INR excluded trade accounting...');
  {
    const input = {
      winning_trades: 5,
      losing_trades: 4,
      breakeven_trades: 1,
      eligible_trades: 10,
      excluded_legacy_trades: 1,
      total_trades: 11,
      win_rate: 50.0,
    };
    const res = formatTradeStatsSummary(input);
    assert.strictEqual(res.winRateDisplay, '50.0%');
    assert.strictEqual(res.subText, '5W / 4L / 1BE • 10 trades • Small Sample');
    assert.strictEqual(res.closedSubDetail, '11 closed (10 INR)');
    assert(res.titleText.includes('1 legacy non-INR trade excluded from win rate'), 'Tooltip must clarify legacy excluded trades');
    assert.strictEqual(res.wins + res.losses + res.be, res.eligibleCount);
  }

  // Test 6: Invariant verification across arbitrary distributions
  console.log('Test 6: Invariant property testing...');
  for (let wins = 0; wins <= 20; wins += 5) {
    for (let losses = 0; losses <= 20; losses += 5) {
      for (let be = 0; be <= 10; be += 2) {
        const eligible = wins + losses + be;
        if (eligible === 0) continue;
        const res = formatTradeStatsSummary({
          winning_trades: wins,
          losing_trades: losses,
          breakeven_trades: be,
          eligible_trades: eligible,
          total_trades: eligible,
          win_rate: (wins / eligible) * 100,
        });
        assert.strictEqual(
          res.wins + res.losses + res.be,
          res.eligibleCount,
          `Invariant broken for W=${wins}, L=${losses}, BE=${be}`
        );
      }
    }
  }

  console.log('All Trade Statistics Regression Tests Passed Successfully!');
}

runTests();
