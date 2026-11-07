# W2 frame: the workspace snapshot's withhold, bound to the signed role (levain, 1107 seat 20 w2)

Unfamiliar construct: binding a derived artifact's parameters to a signed identity the verifier cannot itself look up (the run server holds the
authority replica but not the agent store, so it sees a signed `spec_hash` and nothing under it). Prior art read for the shape, not the code:

1. Nix content-addressed derivations (https://nixos.org/manual/nix/stable/language/advanced-attributes.html and the store-path hashing docs): a consumer trusts an
   output because it re-derives the input hash from declared inputs, not because the producer says so. Taken: the server re-derives the role's withhold
   from the hash chain spec_hash -> role_hash -> role text instead of trusting the `withhold` list the client sends.
2. in-toto layouts (https://in-toto.io/ and the specification at https://github.com/in-toto/docs/blob/master/in-toto-spec.md): a signed layout declares what a step
   may include and a verifier checks each link's materials against it. Taken: the signed object (here the role) carries the allow/deny list; the
   verifier checks the artifact (manifest entries) against it, so the list is honored in the bytes, not just named.
3. Sigstore/cosign attestation verification (https://docs.sigstore.dev/cosign/verifying/attestation/): the verifier is handed the predicate and
   re-hashes it against the signed digest. Taken: evidence rides beside the proof and is re-hashed against the digest the verified signature names.

No code borrowed. UNFETCHED: none of the three pages was fetched in this lane (cited from prior knowledge; the shape is simple and tested here).
