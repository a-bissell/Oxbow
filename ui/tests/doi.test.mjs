// Literature links. OpenAlex hands back DOIs as full resolver URLs; the candidate view used to
// prefix every DOI with https://doi.org/ regardless, so each live link pointed at
// doi.org/https://doi.org/... and resolved nowhere. Run with `npm test` (Node 22+).
import assert from "node:assert/strict";
import { test } from "node:test";
import { doiUrl } from "../src/format.ts";

test("a full resolver URL, as OpenAlex returns it, is used as is", () => {
  assert.equal(doiUrl("https://doi.org/10.1016/j.apcata.2008.12.019"), "https://doi.org/10.1016/j.apcata.2008.12.019");
  assert.equal(doiUrl("http://dx.doi.org/10.1557/JMR.1996.0350"), "https://doi.org/10.1557/JMR.1996.0350");
});

test("a bare identifier gets the resolver prefix", () => {
  assert.equal(doiUrl("10.1063/1.3634052"), "https://doi.org/10.1063/1.3634052");
  assert.equal(doiUrl("  10.1063/1.3634052\n"), "https://doi.org/10.1063/1.3634052");
});

test("legacy identifiers with punctuation still link", () => {
  const sici = "10.1002/(SICI)1097-4636(199806)40:3<358::AID-JBM3>3.0.CO;2-P";
  assert.equal(doiUrl(sici), "https://doi.org/" + sici);
});

test("anything that is not a DOI gets no link at all", () => {
  for (const bad of [null, undefined, "", "   ", "garbage", "10.12/short-prefix", "https://example.com/paper", "10.1063/has space"]) {
    assert.equal(doiUrl(bad), null, JSON.stringify(bad));
  }
});
