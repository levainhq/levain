"""Ruling H1 of the spore -> intention rename: a floor an operator signed under the OLD release still binds.

An authority-log ``raise`` link names a verb by its id and is signed history. The release before the rename signed the
eight ``spore_*`` verb ids; the policy replay maps each recorded id to its current one, in memory, so the signed floor
keeps binding on the verb that exists now. The signed bytes are never rewritten (the signature still verifies over them),
and an old spelling arriving as a NEW link or a NEW ``levain cockpit policy raise`` is refused with a pointer.

The two logs in :data:`OLD_RELEASE_LOGS` are REAL links, written by the code at commit ``a8617e55c`` (the release before
the rename) with throwaway ssh-keygen keys, then dumped row by row together with the effective tier that release computed
for each verb. They are data an old release left on disk, so they are not regenerated from the code under test.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

import levain.authority as auth
from levain import cli
from levain.authority import AuthorityError, ReplayCache, ReplayError
from levain.cockpit.verbs import VerbRegistry
from levain.intention_legacy import OLD_VERB_TO_NEW

OLD_RELEASE_LOGS = json.loads(r'''{"A":{"old_effective":{"pending_cancel":"T2","spore_ascend":"T2","spore_descend":"T3","spore_made_up":"T3","spore_retarget":"T3","spore_seed":"T3","spore_set_disposition":"T2","spore_surface_at":"T3","spore_touch":"T2","spore_update":"T2"},"old_raise_to":{"pending_cancel":"T2","spore_ascend":"T2","spore_descend":"T3","spore_made_up":"T3","spore_retarget":"T3","spore_seed":"T3","spore_set_disposition":"T2","spore_surface_at":"T3","spore_touch":"T2","spore_update":"T2"},"rows":[[1,"","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"class\":\"laptop-presence\",\"custody\":\"passphrase\",\"fingerprint\":\"SHA256:YQAuHsr9KFLPZppJJvPxtgfdebI/T80tZ8dT8NZoVKY\",\"format\":\"levain-authority/1\",\"op\":\"enrol\",\"public_key\":\"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHxS38rs0UyiUWGODy/hVsgboVEtHhg1vLn6TBJr40rX\",\"seq\":1,\"store_id\":\"88565dd1d1c0e78ba2643dc06fdb2fa8\"}","SHA256:YQAuHsr9KFLPZppJJvPxtgfdebI/T80tZ8dT8NZoVKY","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgfFLfyuzRTKJRYY4PL+FWyBuhUS\n0eGDW8ufpMEmvjStcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAECPyDmCvphxg66GWjcDOeBKBhKQnO4jCN4qaaPwYHlP0e7heF\nlwd3fXggvXRuXqotTIIpTECdoaD8cp/8ofLUcJ\n-----END SSH SIGNATURE-----\n","84f9bcf93d0cea4469b7095c645286d66818fcfc640a24299562c327cbbd2288"],[2,"84f9bcf93d0cea4469b7095c645286d66818fcfc640a24299562c327cbbd2288","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"class\":\"display-phone\",\"custody\":\"unstated\",\"fingerprint\":\"SHA256:btzT0YtiNbn2kHPB/R1WhyyGIAMYh02S2L49lTnWL78\",\"format\":\"levain-authority/1\",\"op\":\"enrol\",\"public_key\":\"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAII5SUqUQOBEgjLqs7AfHkMilAR53HSg6wkOaOCc0z9kX\",\"seq\":2,\"store_id\":\"88565dd1d1c0e78ba2643dc06fdb2fa8\"}","SHA256:YQAuHsr9KFLPZppJJvPxtgfdebI/T80tZ8dT8NZoVKY","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgfFLfyuzRTKJRYY4PL+FWyBuhUS\n0eGDW8ufpMEmvjStcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAEBk6XHjJ3i7Ct+nUbpOlvzMCJLZQY7jnjq8v0RFMjvkZqhzHI\nHQJmWCRERnj83JfIWTQmAe6G+0OcRBp8Ds1o4M\n-----END SSH SIGNATURE-----\n","f965697a34a9b2acc1ecbe73d98a40f05db2cbceebd5e3bc75775efcebd30569"],[3,"f965697a34a9b2acc1ecbe73d98a40f05db2cbceebd5e3bc75775efcebd30569","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"format\":\"levain-authority/1\",\"op\":\"raise\",\"seq\":3,\"store_id\":\"88565dd1d1c0e78ba2643dc06fdb2fa8\",\"tier\":\"T2\",\"verb\":\"spore_touch\"}","SHA256:btzT0YtiNbn2kHPB/R1WhyyGIAMYh02S2L49lTnWL78","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgjlJSpRA4ESCMuqzsB8eQyKUBHn\ncdKDrCQ5o4JzTP2RcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAECVvgxmN1Q69Qz5Jcboix7nv4+G1DupS5C32HUVl6ARS4bsvy\nQG3PYREUkzNuGeKcQNkW9ZevHaFGr0CLkQOTUN\n-----END SSH SIGNATURE-----\n","1a6cdd5f3ae523a697b8b96fdd1134652fee2fa314868d93c953581d758dce8d"],[4,"1a6cdd5f3ae523a697b8b96fdd1134652fee2fa314868d93c953581d758dce8d","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"format\":\"levain-authority/1\",\"op\":\"raise\",\"seq\":4,\"store_id\":\"88565dd1d1c0e78ba2643dc06fdb2fa8\",\"tier\":\"T3\",\"verb\":\"spore_descend\"}","SHA256:btzT0YtiNbn2kHPB/R1WhyyGIAMYh02S2L49lTnWL78","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgjlJSpRA4ESCMuqzsB8eQyKUBHn\ncdKDrCQ5o4JzTP2RcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAEBfBoqe602bIPIC1QUMGBpRJ4/re2/eHtFTxZzT6OxHw8VI0a\n3raIxCUGZSFwue38bqyhtcjCc8uwSlhxq3+LcL\n-----END SSH SIGNATURE-----\n","513b771ffd89726386529c4607b4acf6e7a2aaae59c09c93988b460c7c5f81b6"],[5,"513b771ffd89726386529c4607b4acf6e7a2aaae59c09c93988b460c7c5f81b6","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"format\":\"levain-authority/1\",\"op\":\"raise\",\"seq\":5,\"store_id\":\"88565dd1d1c0e78ba2643dc06fdb2fa8\",\"tier\":\"T2\",\"verb\":\"spore_ascend\"}","SHA256:btzT0YtiNbn2kHPB/R1WhyyGIAMYh02S2L49lTnWL78","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgjlJSpRA4ESCMuqzsB8eQyKUBHn\ncdKDrCQ5o4JzTP2RcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAECdXkRS4w1DPYKKS2PvhZXBXpuO0Su2znveiOCp0a/IuJCw9/\nSOTqFuFpTSAaIANJjc4dWd3PR27+ia/J83qVkH\n-----END SSH SIGNATURE-----\n","66820f973ed302a5ba986382176c546fa31b2742ef36474e83299a333251ba72"],[6,"66820f973ed302a5ba986382176c546fa31b2742ef36474e83299a333251ba72","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"format\":\"levain-authority/1\",\"op\":\"raise\",\"seq\":6,\"store_id\":\"88565dd1d1c0e78ba2643dc06fdb2fa8\",\"tier\":\"T3\",\"verb\":\"spore_surface_at\"}","SHA256:btzT0YtiNbn2kHPB/R1WhyyGIAMYh02S2L49lTnWL78","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgjlJSpRA4ESCMuqzsB8eQyKUBHn\ncdKDrCQ5o4JzTP2RcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAEA8ffa92c5on0Bb4J4NhvRggkqz42XEGT+Q3yzJia0bQZdeAJ\nEsg+cfGdVDD/RuVsU50VkZOUqgav4QMChnYOME\n-----END SSH SIGNATURE-----\n","c2548deeb98baf1166c1555d22e19d7fb5ed2b0203aa43dedfb15088a411fd3e"],[7,"c2548deeb98baf1166c1555d22e19d7fb5ed2b0203aa43dedfb15088a411fd3e","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"format\":\"levain-authority/1\",\"op\":\"raise\",\"seq\":7,\"store_id\":\"88565dd1d1c0e78ba2643dc06fdb2fa8\",\"tier\":\"T2\",\"verb\":\"spore_update\"}","SHA256:btzT0YtiNbn2kHPB/R1WhyyGIAMYh02S2L49lTnWL78","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgjlJSpRA4ESCMuqzsB8eQyKUBHn\ncdKDrCQ5o4JzTP2RcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAECVa/3XYQnCHENwNZTdMeEIxhfZ/Zu90FfhA/SDXg6NKIiwm1\nQr+Cw7tbnQeZnqyvJs1JWnWwq3QMNA1tAvrcIA\n-----END SSH SIGNATURE-----\n","f561cee6d6e42b9a5e8ce50a259691d8a185963173f154cf05bffabc652c78f3"],[8,"f561cee6d6e42b9a5e8ce50a259691d8a185963173f154cf05bffabc652c78f3","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"format\":\"levain-authority/1\",\"op\":\"raise\",\"seq\":8,\"store_id\":\"88565dd1d1c0e78ba2643dc06fdb2fa8\",\"tier\":\"T3\",\"verb\":\"spore_retarget\"}","SHA256:btzT0YtiNbn2kHPB/R1WhyyGIAMYh02S2L49lTnWL78","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgjlJSpRA4ESCMuqzsB8eQyKUBHn\ncdKDrCQ5o4JzTP2RcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAECDuDzkzjw1ULF811BApGwNmJP0CUTDaYFKKdQ5Z+UVSkWbrH\nP1Zo2GK65POGPpxQeXDIyiurLEhHCYfFLj38UN\n-----END SSH SIGNATURE-----\n","5c197ea87c6edcf39ce59aeb9a3d6e56f20f944e556f80b58c536c7a8d5f577e"],[9,"5c197ea87c6edcf39ce59aeb9a3d6e56f20f944e556f80b58c536c7a8d5f577e","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"format\":\"levain-authority/1\",\"op\":\"raise\",\"seq\":9,\"store_id\":\"88565dd1d1c0e78ba2643dc06fdb2fa8\",\"tier\":\"T2\",\"verb\":\"spore_set_disposition\"}","SHA256:btzT0YtiNbn2kHPB/R1WhyyGIAMYh02S2L49lTnWL78","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgjlJSpRA4ESCMuqzsB8eQyKUBHn\ncdKDrCQ5o4JzTP2RcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAECnpO4/frm0o07ExCJW47ubKizOn/uMSVT3eqdAQFvlhcqg5b\ng/1dC6BY6WnXUetWt5A/mnRTo7px8ajroasKsI\n-----END SSH SIGNATURE-----\n","a19b48a15f84798e87d5cfef7344f895f156f8f9a290f8064930600d0cfc9a0c"],[10,"a19b48a15f84798e87d5cfef7344f895f156f8f9a290f8064930600d0cfc9a0c","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"format\":\"levain-authority/1\",\"op\":\"raise\",\"seq\":10,\"store_id\":\"88565dd1d1c0e78ba2643dc06fdb2fa8\",\"tier\":\"T3\",\"verb\":\"spore_seed\"}","SHA256:btzT0YtiNbn2kHPB/R1WhyyGIAMYh02S2L49lTnWL78","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgjlJSpRA4ESCMuqzsB8eQyKUBHn\ncdKDrCQ5o4JzTP2RcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAEDOAIpp3KnYuBavadEKcYuo7SmH7/Ih9rgzMjTpqkbv9WxCjr\nT2Krk6TSNLM8SXtLk3WZ6Mxqf+R6n2VZFxuZ0M\n-----END SSH SIGNATURE-----\n","8a3c6f528a231781fb9fa6c7af1da072fa8488dc7f4e0f10f420498a806e65ff"],[11,"8a3c6f528a231781fb9fa6c7af1da072fa8488dc7f4e0f10f420498a806e65ff","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"format\":\"levain-authority/1\",\"op\":\"raise\",\"seq\":11,\"store_id\":\"88565dd1d1c0e78ba2643dc06fdb2fa8\",\"tier\":\"T2\",\"verb\":\"pending_cancel\"}","SHA256:btzT0YtiNbn2kHPB/R1WhyyGIAMYh02S2L49lTnWL78","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgjlJSpRA4ESCMuqzsB8eQyKUBHn\ncdKDrCQ5o4JzTP2RcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAEC+xRn/HFXGSuFbGGE4OSM34/rPuxSN0TlhPSXsaDDAOrKQZA\nBtvC2GR9ps+Z2hFfIEPGEXBsVtBoVjtjfxkXEO\n-----END SSH SIGNATURE-----\n","e1d9a3892484c9bfe663a1a6555e6c46433257259b40a9586f7d35b21e0f80a4"],[12,"e1d9a3892484c9bfe663a1a6555e6c46433257259b40a9586f7d35b21e0f80a4","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"format\":\"levain-authority/1\",\"op\":\"raise\",\"seq\":12,\"store_id\":\"88565dd1d1c0e78ba2643dc06fdb2fa8\",\"tier\":\"T3\",\"verb\":\"spore_made_up\"}","SHA256:btzT0YtiNbn2kHPB/R1WhyyGIAMYh02S2L49lTnWL78","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgjlJSpRA4ESCMuqzsB8eQyKUBHn\ncdKDrCQ5o4JzTP2RcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAEB77RSrnEP2J+WlMOFZpjntC5g9IpE/DyXImWZMM6M3/I0oOz\nrCBTSRqRaRPJb1Owp2esKruX9Ydrept8vGp0QD\n-----END SSH SIGNATURE-----\n","cf063a46cc7ebb2067417d24b3da9230acc297442b48a5ca494c68eb78c485bb"]]},"B":{"old_effective":{"spore_ascend":"T2","spore_descend":"T3","spore_retarget":"C1","spore_seed":"C1","spore_set_disposition":"C1","spore_surface_at":"C1","spore_touch":"C1","spore_update":"C1"},"old_raise_to":{"spore_ascend":"T2","spore_descend":"T3"},"rows":[[1,"","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"class\":\"laptop-presence\",\"custody\":\"passphrase\",\"fingerprint\":\"SHA256:YQAuHsr9KFLPZppJJvPxtgfdebI/T80tZ8dT8NZoVKY\",\"format\":\"levain-authority/1\",\"op\":\"enrol\",\"public_key\":\"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHxS38rs0UyiUWGODy/hVsgboVEtHhg1vLn6TBJr40rX\",\"seq\":1,\"store_id\":\"9df5b4a751ab83381118e1035789f16a\"}","SHA256:YQAuHsr9KFLPZppJJvPxtgfdebI/T80tZ8dT8NZoVKY","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgfFLfyuzRTKJRYY4PL+FWyBuhUS\n0eGDW8ufpMEmvjStcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAEBxPKbdbk2dbDqbitVfW4FFnunATXK0qKL8TjWz8BdXQZohI/\nb2S+gIFNcDt7feeE+fI88TEa0Ps0KGMDDSou8H\n-----END SSH SIGNATURE-----\n","45bdd2f87042c0394df1ace1a4186b9b69e49d57f69f622fb2adc315c50799af"],[2,"45bdd2f87042c0394df1ace1a4186b9b69e49d57f69f622fb2adc315c50799af","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"class\":\"display-phone\",\"custody\":\"unstated\",\"fingerprint\":\"SHA256:btzT0YtiNbn2kHPB/R1WhyyGIAMYh02S2L49lTnWL78\",\"format\":\"levain-authority/1\",\"op\":\"enrol\",\"public_key\":\"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAII5SUqUQOBEgjLqs7AfHkMilAR53HSg6wkOaOCc0z9kX\",\"seq\":2,\"store_id\":\"9df5b4a751ab83381118e1035789f16a\"}","SHA256:YQAuHsr9KFLPZppJJvPxtgfdebI/T80tZ8dT8NZoVKY","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgfFLfyuzRTKJRYY4PL+FWyBuhUS\n0eGDW8ufpMEmvjStcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAEBTL1eNacLkLb5h+z/7hzKkO3u0lX9dEb8Ga50iOTAcsu6XfO\n5eNttuokgGZJoqmU3jCAv8mgzLyw6uGaHWsTwF\n-----END SSH SIGNATURE-----\n","a546e9ea838eeeefe9669c34d2b8426ea3e937bb39d0a4a445c78d8aba87443a"],[3,"a546e9ea838eeeefe9669c34d2b8426ea3e937bb39d0a4a445c78d8aba87443a","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"format\":\"levain-authority/1\",\"op\":\"raise\",\"seq\":3,\"store_id\":\"9df5b4a751ab83381118e1035789f16a\",\"tier\":\"T2\",\"verb\":\"spore_touch\"}","SHA256:btzT0YtiNbn2kHPB/R1WhyyGIAMYh02S2L49lTnWL78","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgjlJSpRA4ESCMuqzsB8eQyKUBHn\ncdKDrCQ5o4JzTP2RcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAEAr9yN41b/mF+6lmxgNwCzNaGBDPG9KWKVmfvgc/FaCCQeJjI\nqZbJm5BIM0MR44ITXWqQ4BuE0EV/jAbksFQNQK\n-----END SSH SIGNATURE-----\n","91ea360e7ac0e535ee079ca7da97fccc3945be80670724ee2ac1b0897ab7c967"],[4,"91ea360e7ac0e535ee079ca7da97fccc3945be80670724ee2ac1b0897ab7c967","{\"at\":\"2026-11-01T09:00:00.000000Z\",\"format\":\"levain-authority/1\",\"op\":\"loosen\",\"policy\":{\"grants\":{},\"raise_to\":{\"spore_ascend\":\"T2\",\"spore_descend\":\"T3\"},\"timeouts\":{}},\"seq\":4,\"store_id\":\"9df5b4a751ab83381118e1035789f16a\"}","SHA256:YQAuHsr9KFLPZppJJvPxtgfdebI/T80tZ8dT8NZoVKY","-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgfFLfyuzRTKJRYY4PL+FWyBuhUS\n0eGDW8ufpMEmvjStcAAAAQbGV2YWluLWF1dGhvcml0eQAAAAAAAAAGc2hhNTEyAAAAUwAA\nAAtzc2gtZWQyNTUxOQAAAEB+J3ejq5aSuY5pj3ywiyfYkJt0KgkxdqaWp+FIIQgIn/EVkg\niahgLI9WMwXecfr92m7pIRXk3JaJntstp0q3cE\n-----END SSH SIGNATURE-----\n","bcb8760b06fadb2e925fbb1b7f423d9fcb250268fa8426efdc5ab7a5b3cb2261"]]},"old_commit":"a8617e55c"}''')

# every verb A raises, in the order A raises them: the eight the old registry served, then two the map must not touch
OLD_VERBS = ("spore_touch", "spore_descend", "spore_ascend", "spore_surface_at", "spore_update", "spore_retarget",
             "spore_set_disposition", "spore_seed")
LATER = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def _load(which: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    auth.create_schema(conn)
    for row in OLD_RELEASE_LOGS[which]["rows"]:
        conn.execute("INSERT INTO authority_log VALUES (?, ?, ?, ?, ?, ?)", row)
    return conn


def _replay(conn: sqlite3.Connection) -> auth.AuthoritySnapshot:
    return auth.snapshot(conn, cache=ReplayCache())


def _raw_rows(conn: sqlite3.Connection) -> list[list]:
    return [list(r) for r in conn.execute(
        "SELECT seq, prev_hash, record, signer_fp, signature, hash FROM authority_log ORDER BY seq")]


# --- the fixture is what it claims to be ------------------------------------------------------------------------------


def test_the_fixture_logs_are_old_release_links_naming_every_old_verb():
    recorded = {json.loads(row[2])["verb"] for row in OLD_RELEASE_LOGS["A"]["rows"] if json.loads(row[2])["op"] == "raise"}
    assert set(OLD_VERBS) <= recorded, "log A must hold a raise of every old registry verb"
    assert all(v.startswith("spore_") for v in OLD_VERBS)
    assert OLD_RELEASE_LOGS["old_commit"] == "a8617e55c"


# --- the legacy table is total ----------------------------------------------------------------------------------------


def test_the_legacy_table_is_total_over_the_old_registry_and_lands_on_the_new_registry():
    """The old registry served exactly :data:`OLD_VERBS` as ``spore_*`` verbs (read from the release before the rename
    when this test was written); the table maps each, injectively, onto a verb the NEW registry serves."""
    assert set(OLD_VERB_TO_NEW) == set(OLD_VERBS)
    new_ids = list(OLD_VERB_TO_NEW.values())
    assert len(set(new_ids)) == len(new_ids), "two old verbs map to one new verb: a floor would merge"
    served = set(VerbRegistry().names())
    assert set(new_ids) <= served, f"a mapped id the new registry does not serve: {set(new_ids) - served}"
    # the verb map: descend -> close and ascend -> promote, never a prefix swap
    assert OLD_VERB_TO_NEW["spore_descend"] == "intention_close"
    assert OLD_VERB_TO_NEW["spore_ascend"] == "intention_promote"
    # nothing the new registry serves is still spelled with the old name
    assert not [v for v in served if v.startswith("spore")]


# --- the replay keeps the signed floor binding ------------------------------------------------------------------------


def test_a_floor_signed_under_the_old_release_binds_the_new_verb_at_the_same_tier():
    snap = _replay(_load("A"))
    tiers = OLD_RELEASE_LOGS["A"]["old_effective"]
    assert {tiers[v] for v in OLD_VERBS} == {"T2", "T3"}, "the fixture must hold both tiers or a swapped map passes"
    for old, new in OLD_VERB_TO_NEW.items():
        assert snap.policy.apply(new, "C1") == tiers[old], f"{old} -> {new}"
        # a floor only raises, whatever the base is
        assert snap.policy.apply(new, "T3") == "T3"


def test_the_replayed_policy_holds_each_floor_under_its_new_id_only():
    snap = _replay(_load("A"))
    held = dict(snap.policy.raise_to)
    assert not set(held) & set(OLD_VERB_TO_NEW), "an old spelling survived into the replayed policy"
    assert {OLD_VERB_TO_NEW[v] for v in OLD_VERBS} <= set(held)


def test_a_verb_outside_the_map_keeps_its_recorded_name():
    """The negative control: only a mapped id moves. A verb that is not a spore verb is untouched, and a ``spore_``
    id the old registry never had is recorded under the name it was signed with (as it always was), so the map is not
    a prefix rule."""
    snap = _replay(_load("A"))
    assert snap.policy.raise_to["pending_cancel"] == "T2"
    assert snap.policy.raise_to["spore_made_up"] == "T3"
    assert "intention_made_up" not in snap.policy.raise_to
    assert snap.policy.apply("spore_made_up", "C1") == "T3"


def test_a_loosen_that_records_old_verbs_binds_the_new_ones_as_the_old_release_computed():
    snap = _replay(_load("B"))
    tiers = OLD_RELEASE_LOGS["B"]["old_effective"]
    assert {tiers[v] for v in OLD_VERBS} == {"C1", "T2", "T3"}
    for old, new in OLD_VERB_TO_NEW.items():
        assert snap.policy.apply(new, "C1") == tiers[old], f"{old} -> {new}"
    assert not set(snap.policy.raise_to) & set(OLD_VERB_TO_NEW)


# --- the signed bytes are history -------------------------------------------------------------------------------------


def test_replay_verifies_the_signatures_over_the_original_bytes_and_rewrites_nothing():
    conn = _load("A")
    before = _raw_rows(conn)
    snap = auth.snapshot(conn, cache=ReplayCache())         # check_signature is on: every signature is verified
    assert snap.seq == len(before)
    assert _raw_rows(conn) == before
    assert before == OLD_RELEASE_LOGS["A"]["rows"]


def test_rewriting_a_signed_record_to_the_new_spelling_breaks_its_signature():
    """The control for the test above: the signature covers the old spelling, so the only place a verb may be mapped
    is in memory. The last link is rewritten with its hash re-chained and its signature left alone."""
    conn = _load("A")
    seq, prev, record, fp, signature, _ = _raw_rows(conn)[-1]
    forged = record.replace("spore_made_up", "intention_made_up")
    assert forged != record
    conn.execute("UPDATE authority_log SET record = ?, hash = ? WHERE seq = ?",
                 (forged, auth._link_hash(prev, forged), seq))
    with pytest.raises(ReplayError, match="signature does not verify"):
        _replay(conn)


# --- a NEW raise of an old spelling refuses ---------------------------------------------------------------------------


def _phone_fp(conn: sqlite3.Connection) -> str:
    return next(json.loads(row[2])["fingerprint"] for row in _raw_rows(conn)
                if json.loads(row[2]).get("class") == "display-phone")


@pytest.mark.parametrize("old,new", sorted(OLD_VERB_TO_NEW.items()))
def test_a_new_raise_of_an_old_verb_is_refused_naming_the_new_id(old, new):
    conn = _load("A")
    with pytest.raises(AuthorityError, match=new):
        auth.prepare(conn, {"op": "raise", "verb": old, "tier": "T3"}, signer_fp=_phone_fp(conn), now=LATER,
                     cache=ReplayCache())


def test_a_new_raise_of_the_new_id_is_accepted_where_the_old_one_is_refused():
    conn = _load("B")
    link = auth.prepare(conn, {"op": "raise", "verb": "intention_touch", "tier": "T3"}, signer_fp=_phone_fp(conn),
                        now=LATER, cache=ReplayCache())
    assert json.loads(link.record)["verb"] == "intention_touch"


def test_a_new_loosen_that_names_an_old_verb_is_refused_too():
    conn = _load("B")
    laptop = next(json.loads(row[2])["fingerprint"] for row in _raw_rows(conn)
                  if json.loads(row[2]).get("class") == "laptop-presence")
    policy = {"raise_to": {"spore_descend": "T2"}, "timeouts": {}, "grants": {}}
    with pytest.raises(AuthorityError, match="intention_close"):
        auth.prepare(conn, {"op": "loosen", "policy": policy}, signer_fp=laptop, now=LATER, cache=ReplayCache())


@pytest.mark.parametrize("old,new", sorted(OLD_VERB_TO_NEW.items()))
def test_the_policy_raise_command_refuses_an_old_verb_with_exit_2_and_the_new_id(old, new, tmp_path, capsys):
    code = cli.main(["cockpit", "policy", "raise", old, "T3", "--signing-key", str(tmp_path / "no-such-key"),
                     "--data-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2, err
    assert new in err and old in err


def test_the_policy_raise_command_still_reaches_the_key_for_an_unmapped_id(tmp_path, capsys):
    """The control: only a retired id short-circuits to exit 2; any other verb goes on to the signer as before."""
    code = cli.main(["cockpit", "policy", "raise", "intention_touch", "T3", "--signing-key",
                     str(tmp_path / "no-such-key"), "--data-root", str(tmp_path)])
    assert code == 1, capsys.readouterr().err
