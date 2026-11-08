const assert = require('node:assert/strict');
const {
  getInstitutionalFlowNet,
  formatInstitutionalNetCr,
} = require('../dashboard/tradeflow_helper.js');

const nsePayload = {
  source: 'NSE',
  real_data: true,
  flows: [
    { category: 'FII/FPI', net_cr: -0.25 },
    { category: 'DII', net_cr: -700.31 },
    { category: 'Estimated Other / Residual', net_cr: 700.56 },
  ],
};

assert.strictEqual(getInstitutionalFlowNet(nsePayload, 'FII/FPI'), -0.25);
assert.strictEqual(getInstitutionalFlowNet(nsePayload, 'FII'), -0.25);
assert.strictEqual(getInstitutionalFlowNet(nsePayload, 'DII'), -700.31);
assert.strictEqual(formatInstitutionalNetCr(-0.25), '-₹0.25 Cr');
assert.strictEqual(formatInstitutionalNetCr(-700.31), '-₹700.31 Cr');

const zeroPayload = { flows: [{ category: 'FII/FPI', net_cr: 0 }, { category: 'DII', net_cr: '0' }] };
assert.strictEqual(getInstitutionalFlowNet(zeroPayload, 'FII/FPI'), 0);
assert.strictEqual(formatInstitutionalNetCr(0), '₹0.00 Cr');
assert.strictEqual(formatInstitutionalNetCr(getInstitutionalFlowNet(zeroPayload, 'DII')), '₹0.00 Cr');

assert.strictEqual(getInstitutionalFlowNet({ flows: [] }, 'FII/FPI'), null);
assert.strictEqual(getInstitutionalFlowNet({ fii_net: 3114.68, dii_net: -392.2 }, 'FII/FPI'), null);
assert.strictEqual(formatInstitutionalNetCr(null), '—');
assert.strictEqual(formatInstitutionalNetCr('not-a-number'), '—');

console.log('All institutional flow helper regression tests passed.');
