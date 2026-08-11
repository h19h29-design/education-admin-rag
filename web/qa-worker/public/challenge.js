const CHALLENGE_RE = /^senqa-pow-[0-9a-f]{32}$/u;
const MAX_NONCE = 2_000_000;

function hasLeadingZeroBits(bytes, difficultyBits) {
  for (let bit = 0; bit < difficultyBits; bit += 1) {
    if ((bytes[Math.floor(bit / 8)] & (1 << (7 - (bit % 8)))) !== 0) return false;
  }
  return true;
}

export async function solveFirstPartyChallenge(value) {
  if (
    !value ||
    value.constructor !== Object ||
    !CHALLENGE_RE.test(value.challenge_id) ||
    !Number.isInteger(value.difficulty_bits) ||
    value.difficulty_bits < 8 ||
    value.difficulty_bits > 20
  ) {
    throw new Error("challenge_invalid");
  }
  const encoder = new TextEncoder();
  for (let nonce = 0; nonce <= MAX_NONCE; nonce += 1) {
    const input = encoder.encode(`${value.challenge_id}:${nonce}`);
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", input));
    if (hasLeadingZeroBits(digest, value.difficulty_bits)) {
      return `pow:${value.challenge_id}:${nonce}`;
    }
  }
  throw new Error("challenge_unsolved");
}
