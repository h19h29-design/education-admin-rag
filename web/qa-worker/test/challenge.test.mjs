import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { solveFirstPartyChallenge } from "../public/challenge.js";

function hasLeadingZeroBits(value, difficultyBits) {
  const digest = createHash("sha256").update(value).digest();
  for (let bit = 0; bit < difficultyBits; bit += 1) {
    if ((digest[Math.floor(bit / 8)] & (1 << (7 - (bit % 8)))) !== 0) return false;
  }
  return true;
}

test("solves a bounded first-party proof without external scripts", async () => {
  const challengeId = "senqa-pow-07070707070707070707070707070707";
  const proof = await solveFirstPartyChallenge({
    challenge_id: challengeId,
    difficulty_bits: 8,
  });

  assert.match(proof, /^pow:senqa-pow-[0-9a-f]{32}:[0-9]+$/);
  const nonce = proof.split(":").at(-1);
  assert.equal(hasLeadingZeroBits(`${challengeId}:${nonce}`, 8), true);
});

test("rejects malformed or excessive challenge work", async () => {
  await assert.rejects(
    solveFirstPartyChallenge({ challenge_id: "wrong", difficulty_bits: 8 }),
    /challenge_invalid/,
  );
  await assert.rejects(
    solveFirstPartyChallenge({
      challenge_id: "senqa-pow-07070707070707070707070707070707",
      difficulty_bits: 31,
    }),
    /challenge_invalid/,
  );
});
