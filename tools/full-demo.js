// tools/full-demo.js
// Complete end-to-end TerraLedger demo (no UI, pure on-chain):
//   Mint   (ProjectRegistry → Oracle verification → TLCERT soulbound + TCC)
//   List   (500 TCC on TCCMarketplace @ 10 QUSDC/tonne)
//   Retire (200 TCC permanently burnt, CO₂ offset recorded on-chain)

require('dotenv').config()
const { ethers } = require('../node_modules/ethers')

const RPC = 'https://rpc1mainnet.qie.digital/'
const PK  = process.env.PRIVATE_KEY || process.env.MAINNET_PRIVATE_KEY

const ADDR = {
  ProjectRegistry:   '0x6595944383d1ea78473E44466a2eC247f5560349',
  CarbonCredit:      '0x4F998ED896659117101A29E4c74C7585365734B0',
  CarbonOracle:      '0x9D77C7fEEb5867a51870655e05F9dD06B8139fC1',
  QUSDC:             '0x3F43DA82eC9A4f5285F10FaF1F26EcA7319E5DA5',
  QIEPass:           '0x97Ed3C1A93e0a49C37D32435B9848ADcd5cdaBAf',
  CarbonCreditToken: '0x880B6bdF82FCce76fdc92393b12A10aCbb509E93',
  TCCMarketplace:    '0x45DD6AeA3a35Ea7B22EcBc3A10f7fC495696f116',
}

const REGISTRY_ABI = [
  'function submitProject(bytes32 id,string polygonGeoJSON,uint256 areaHectares,uint256 claimedTonnes,int64 minLat,int64 minLng,int64 maxLat,int64 maxLng) external',
  'function getProject(bytes32 id) view returns (tuple(bytes32 id,address owner,string polygonGeoJSON,uint256 areaHectares,uint256 claimedTonnes,uint256 submittedAt,uint8 status,string statusReason,int64 minLat,int64 minLng,int64 maxLat,int64 maxLng))',
]
const ORACLE_ABI = [
  'function submitVerification(bytes32 projectId,uint256 gpsScore,uint256 ownershipScore,uint256 anomalyScore,uint256 satelliteScore,bool gpsHardFail,bool ownershipHardFail,string reportIpfsCid,string[] flags,uint256 vintage,uint256 tonnes,bytes32 docHash) external',
  'function finalized(bytes32) view returns (bool)',
]
const TCC_ABI = [
  'function balanceOf(address) view returns (uint256)',
  'function approve(address,uint256) returns (bool)',
  'function retire(uint256 amount,bytes32 projectId) external',
  'event CreditsRetired(address indexed by,uint256 amount,bytes32 indexed projectId)',
]
const TLCERT_ABI = [
  'function balanceOf(address) view returns (uint256)',
]
const TCC_MKT_ABI = [
  'function list(uint256 amount,uint256 pricePerTonne) external returns (uint256)',
  'function buy(uint256 listingId,uint256 amount) external',
  'function getActiveListings() view returns (uint256[],address[],uint256[],uint256[])',
  'function totalVolume() view returns (uint256)',
  'function totalSales() view returns (uint256)',
  'event Listed(uint256 indexed listingId,address indexed seller,uint256 amount,uint256 pricePerTonne)',
  'event Sold(uint256 indexed listingId,address indexed seller,address indexed buyer,uint256 amount,uint256 quscPaid,uint256 fee)',
]
const QUSDC_ABI = [
  'function balanceOf(address) view returns (uint256)',
  'function faucet() external',
  'function transfer(address,uint256) returns (bool)',
  'function approve(address,uint256) returns (bool)',
]

const sep = t => console.log(`\n${'─'.repeat(60)}\n  ${t}\n${'─'.repeat(60)}`)
const log = (k, v) => console.log(`  ${String(k).padEnd(32)} ${v}`)
const fmt6 = n => (Number(n) / 1e6).toFixed(2) + ' QUSDC'

async function sendTx(contract, method, args, gasLimit = 500000) {
  const tx = await contract[method](...args, { gasLimit })
  process.stdout.write(`  tx ${tx.hash.slice(0, 18)}… `)
  const rcpt = await tx.wait()
  console.log(`✓  (gas: ${rcpt.gasUsed})`)
  return { tx, rcpt }
}

async function main() {
  const provider = new ethers.JsonRpcProvider(RPC)
  const seller   = new ethers.Wallet(PK, provider)

  sep('0  Wallet')
  log('Oracle/deployer wallet:', seller.address)
  const sellerQIE = await provider.getBalance(seller.address)
  log('QIE balance:', ethers.formatEther(sellerQIE) + ' QIE')

  // ── contracts ────────────────────────────────────────────────────────────
  const registry = new ethers.Contract(ADDR.ProjectRegistry,   REGISTRY_ABI, seller)
  const oracle   = new ethers.Contract(ADDR.CarbonOracle,       ORACLE_ABI,   seller)
  const tcc      = new ethers.Contract(ADDR.CarbonCreditToken,  TCC_ABI,      seller)
  const tccMkt   = new ethers.Contract(ADDR.TCCMarketplace,     TCC_MKT_ABI,  seller)
  const tlcert   = new ethers.Contract(ADDR.CarbonCredit,       TLCERT_ABI,   seller)

  // ── Step 1: register project ─────────────────────────────────────────────
  sep('1  Register project on ProjectRegistry')

  const ts        = Date.now()
  const projectId = ethers.encodeBytes32String(`TL-${ts}`.slice(0, 31))
  const polygon   = '{"type":"Polygon","coordinates":[[[77.5,28.6],[77.6,28.6],[77.6,28.7],[77.5,28.7],[77.5,28.6]]]}'
  const tonnes    = 1000n
  const hectares  = 500n
  const minLat = Math.round(28.6 * 1e6)
  const minLng = Math.round(77.5 * 1e6)
  const maxLat = Math.round(28.7 * 1e6)
  const maxLng = Math.round(77.6 * 1e6)

  log('Project ID (bytes32):', projectId)
  log('Location:', 'Uttarakhand, India (28.6–28.7°N, 77.5–77.6°E)')
  log('Area:', '500 ha')
  log('Claimed tonnes:', '1000 TCC')

  process.stdout.write('  submitProject ')
  await sendTx(registry, 'submitProject',
    [projectId, polygon, hectares, tonnes, minLat, minLng, maxLat, maxLng])

  const proj = await registry.getProject(projectId)
  log('Registered owner:', proj.owner)
  log('Status:', ['Pending','Approved','Rejected','FraudFlagged'][Number(proj.status)])

  // ── Step 2: oracle verification → mint TLCERT + TCC ──────────────────────
  sep('2  Oracle verification  →  TLCERT (soulbound) + TCC mint')

  const gpsScore       = 28   // /30
  const ownershipScore = 23   // /25
  const anomalyScore   = 24   // /25
  const satelliteScore = 20   // /20  → total 95
  const total          = gpsScore + ownershipScore + anomalyScore + satelliteScore
  const docHash        = ethers.keccak256(ethers.toUtf8Bytes('demo_deed_2024.pdf'))
  const ipfsCid        = 'bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi'

  log('Scores:', `GPS ${gpsScore}/30 | Own ${ownershipScore}/25 | Anom ${anomalyScore}/25 | Sat ${satelliteScore}/20`)
  log('Total:', `${total}/100  (pass ≥ 70 ✓)`)
  log('Hard fails:', 'none')

  const tccBefore   = await tcc.balanceOf(seller.address)
  const certsBefore = await tlcert.balanceOf(seller.address)

  process.stdout.write('  submitVerification ')
  const { rcpt: rcpt2 } = await sendTx(oracle, 'submitVerification',
    [projectId, gpsScore, ownershipScore, anomalyScore, satelliteScore,
     false, false, ipfsCid, [], 2024, tonnes, docHash], 900000)

  const tccAfter   = await tcc.balanceOf(seller.address)
  const certsAfter = await tlcert.balanceOf(seller.address)

  log('TLCERT minted:', `${certsAfter - certsBefore} certificate (soulbound)`)
  log('TCC minted:', `${tccAfter - tccBefore} TCC  (= ${tccAfter - tccBefore} tonnes CO₂)`)
  log('Seller TCC balance:', tccAfter.toString() + ' TCC')

  // ── Step 3: list 500 TCC on TCCMarketplace ───────────────────────────────
  sep('3  List 500 TCC on TCCMarketplace  @  10 QUSDC / tonne')

  const listAmt        = 500n
  const pricePerTonne  = ethers.parseUnits('10', 6)   // 10 QUSDC per TCC

  process.stdout.write('  approve TCC → TCCMarketplace ')
  await sendTx(tcc, 'approve', [ADDR.TCCMarketplace, listAmt], 100000)

  process.stdout.write('  list ')
  const { rcpt: rcpt6 } = await sendTx(tccMkt, 'list', [listAmt, pricePerTonne], 300000)

  const listedEv = rcpt6.logs
    .map(l => { try { return tccMkt.interface.parseLog(l) } catch { return null } })
    .find(e => e?.name === 'Listed')
  const listingId = listedEv ? Number(listedEv.args.listingId) : 0

  log('Listing ID:', listingId)
  log('Listed:', `${listAmt} TCC @ 10 QUSDC/tonne  (total value: ${fmt6(listAmt * 10n * 1_000_000n)})`)

  const [ids] = await tccMkt.getActiveListings()
  log('Active listings on-chain:', ids.length.toString())
  log('Note:', 'Buy step skipped — needs real QUSDC buyer on mainnet')

  // ── Step 4: retire 200 TCC ───────────────────────────────────────────────
  sep('4  Retire 200 TCC  (permanent CO₂ offset, tokens burned)')

  const retireAmt = 200n

  process.stdout.write('  retire ')
  await sendTx(tcc, 'retire', [retireAmt, projectId], 200000)

  const finalSellerTCC = await tcc.balanceOf(seller.address)
  log('TCC burnt (offset):', retireAmt.toString() + ' TCC  =  200 tonnes CO₂ permanently offset')
  log('Seller TCC balance (after retire):', finalSellerTCC.toString() + ' TCC')

  // ── Final summary ─────────────────────────────────────────────────────────
  sep('SUMMARY')
  log('TLCERT soulbound certs:', (await tlcert.balanceOf(seller.address)).toString() + ' certificate(s)')
  log('TCC wallet balance:', finalSellerTCC.toString() + ' TCC  (in hand)')
  log('TCC listed (escrowed):', listAmt.toString() + ' TCC  (listing #' + listingId + ')')
  log('TCC retired (burnt):', retireAmt.toString() + ' TCC  → permanent CO₂ offset')
  log('Total accounted for:', (finalSellerTCC + listAmt + retireAmt).toString() + ' / 1000 TCC minted')

  const EX = 'https://mainnet.qie.digital'
  console.log('\n  Explorer links:')
  console.log(`  TLCERT contract : ${EX}/address/${ADDR.CarbonCredit}`)
  console.log(`  TCC contract    : ${EX}/address/${ADDR.CarbonCreditToken}`)
  console.log(`  TCCMarketplace  : ${EX}/address/${ADDR.TCCMarketplace}`)
}

main().catch(e => { console.error('\n' + e.message); process.exit(1) })
