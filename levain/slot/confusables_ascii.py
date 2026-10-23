"""Single code points that look like an ASCII character of a CONTRACT §10.3 surface name, for
:func:`levain.slot.adopt_preview.surface_lookalike` (a ``.cl\u0430ude`` with a Cyrillic ``\u0430``).

GENERATED, do not edit: ``/usr/bin/python3 project_memory/1022-21/adopt5dii/s2f1_gen_confusables.py <confusables.txt>
<this file>`` over Unicode's ``security/18.0.0/confusables.txt`` ("# Date: 2026-08-06, 01:05:35 GMT",
sha256 ``6ed3ee967c9dfdf6677d563c9985182fbc50a2efb7d6059cd57b2e2ce18f5b92``). Kept: each entry whose source is ONE
non-ASCII code point and whose prototype is ONE character that casefolds to ``[a-z0-9._-]``, minus the sources NFKC +
casefold already fold to it (on the floor python's unicodedata, so the table is a superset of what a newer one
leaves). Multi-character prototypes (``rn`` for ``m``) are not kept. The data is derived from Unicode data files,
Copyright (c) Unicode, Inc., used under the Unicode License v3 (https://www.unicode.org/license.txt). Count:
``python3 -c "from levain.slot import confusables_ascii as C; print(len(C.LOOKALIKE))"``.
"""
from __future__ import annotations

from typing import Dict

#: Prototype character -> the code points confusables.txt maps to it.
_BY_TARGET: Dict[str, str] = {
    "-": "\u02d7\u06d4\u174d\u1bf3\u2010\u2011\u2012\u2013\u2043\u2212\u2796\u2cba\u2cbb\uaa7d\ufe58\U00010191",
    ".": "\u0660\u06f0\u0701\u0702\ua4f8\ua60e\uabec\U00010a50\U0001d16d\U0001ecae",
    "2": "\u01a7\u03e8\u10c7\u14bf\u1616\u1cb7\ua644\ua6ef\ua75a\U0001ccf2",
    "3": "\u01b7\u021c\u0417\u04e0\u0545\u0969\u0ae9\u1702\u1c95\u2c9c\u2cc4\u2ccc\ua76a\ua7ab\U000118ca\U00016f3b"
           "\U0001ccf3\U0001d206",
    "4": "\u13ce\uab9e\U000118af\U00016e82\U0001ccf4",
    "5": "\u01bc\U000118bb\U0001ccf5",
    "6": "\u03ec\u0431\u13ee\u2cd2\u2cd3\u2cdc\ua543\uabbe\U000118d5\U0001ccf6",
    "7": "\u172a\u1c84\U000104d2\U000118c6\U00016e8a\U0001ccf7\U0001d1fe\U0001d212",
    "8": "\u0222\u0223\u09ea\u0a6a\u0b03\ua589\U0001031a\U00011445\U000169fe\U0001ccf8\U0001e8cb",
    "9": "\u09ed\u0a67\u0b68\u0d6d\u2cca\u2ccb\ua76e\ua76f\U000118ac\U000118cc\U000118d6\U000169c1\U0001ccf9"
           "\U0001e2f2",
    "_": "\u0640\u07fa\U000145c1",
    "a": "\u0251\u0391\u03b1\u0410\u0430\u13aa\u15c5\u237a\ua4ee\uab64\U000102a0\U00016f40\U0001ccd6\U0001d6a8"
           "\U0001d6c2\U0001d6e2\U0001d6fc\U0001d71c\U0001d736\U0001d756\U0001d770\U0001d790\U0001d7aa\U0001df5a"
           "\U0001df6a",
    "b": "\u0184\u0392\u0412\u042c\u07d5\u13cf\u13f4\u1472\u15af\u15f7\u2c82\ua4d0\ua557\ua7b4\U00010282\U000102a1"
           "\U00010301\U0001031c\U00016eb6\U0001ccd7\U0001d207\U0001d6a9\U0001d6e3\U0001d71d\U0001d757\U0001d791",
    "c": "\u03f2\u03f9\u0421\u0441\u1004\u105a\u13df\u1c83\u1d04\u2ca4\u2ca5\ua4da\uabaf\U000102a2\U00010302"
           "\U00010415\U0001043d\U0001051b\U000118e9\U000118f2\U0001ccd8\U0001d134\U0001f74c",
    "d": "\u0501\u13a0\u13e7\u146f\u15de\u15ea\ua4d2\ua4d3\U0001018b\U0001ccd9",
    "e": "\u0395\u0415\u0435\u04bd\u13ac\u212e\u22ff\u2d39\ua4f0\ua5cb\uab32\U00010286\U000118a6\U000118ae\U0001ccda"
           "\U0001d6ac\U0001d6e6\U0001d720\U0001d75a\U0001d794\U0001df81",
    "f": "\u017f\u0192\u0284\u03dc\u0584\u07d3\u15b4\u1e9d\ua4dd\ua798\ua799\uab35\U0001017e\U00010287\U000102a5"
           "\U00010525\U000118a2\U000118c2\U0001ccdb\U0001d213\U0001d7ca",
    "g": "\u018d\u0261\u050c\u0581\u13c0\u13f3\u1d83\ua4d6\U0001ccdc",
    "h": "\u0397\u041d\u04ba\u04bb\u0570\u10b9\u13bb\u13c2\u157c\u2c8e\ua4e7\U000102cf\U0001ccdd\U0001d6ae\U0001d6e8"
           "\U0001d722\U0001d75c\U0001d796",
    "i": "\u00a1\u0131\u0269\u026a\u03b9\u0456\u0582\u13a5\u2373\u2c93\ua647\uab75\U000118c3\U0001d6a4\U0001d6ca"
           "\U0001d704\U0001d73e\U0001d778\U0001d7b2",
    "j": "\u0237\u037f\u03f3\u0408\u0458\u0575\u13ab\u148d\ua4d9\ua7b2\U0001ccdf\U0001d6a5",
    "k": "\u039a\u041a\u13e6\u16d5\u2c94\ua4d7\U00010518\U0001cce0\U0001d6b1\U0001d6eb\U0001d725\U0001d75f\U0001d799",
    "l": "\u0196\u01c0\u0399\u0406\u04c0\u04cf\u05c0\u05d5\u05df\u0627\u0661\u06f1\u07ca\u0964\u104a\u13de\u14aa"
           "\u16c1\u16d0\u2110\u2111\u2160\u21bf\u2223\u23fd\u2502\u2503\u2c92\u2cd0\u2d4a\u2d4f\ua4e1\ua4f2\ua56f"
           "\ua781\ua7ae\ua7fe\ua830\ua8ce\uaa5d\ufe31\ufe8d\ufe8e\uff11\uff29\uff5c\uffe8\U0001028a\U00010309"
           "\U00010320\U0001041b\U0001050e\U00010526\U00010926\U00010c3e\U00010ca5\U00010cfa\U00011047\U000110c0"
           "\U00011141\U000111c5\U000113d4\U0001144b\U000115c5\U00011641\U000118a3\U000118b2\U00011c41\U00011dda"
           "\U00011de1\U00016d63\U00016eaa\U00016f16\U00016f28\U0001ccde\U0001cce1\U0001ccf1\U0001d100\U0001d22a"
           "\U0001d377\U0001d408\U0001d43c\U0001d470\U0001d4d8\U0001d540\U0001d574\U0001d5a8\U0001d5dc\U0001d610"
           "\U0001d644\U0001d678\U0001d6b0\U0001d6ea\U0001d724\U0001d75e\U0001d798\U0001d7cf\U0001d7d9\U0001d7e3"
           "\U0001d7ed\U0001d7f7\U0001e141\U0001e8c7\U0001ed01\U0001ee00\U0001ee80\U0001fbf1",
    "m": "\u039c\u03fa\u041c\u13b7\u15f0\u16d6\u2c98\ua4df\U000102b0\U00010311\U00010c21\U0001cce2\U0001d6b3"
           "\U0001d6ed\U0001d727\U0001d761\U0001d79b",
    "n": "\u039d\u0578\u057c\u2c9a\ua4e0\U00010513\U00011abe\U0001cce3\U0001d6b4\U0001d6ee\U0001d728\U0001d762"
           "\U0001d79c",
    "o": "\u039f\u03bf\u03c3\u03ed\u041e\u043e\u0555\u0585\u05e1\u0647\u0665\u06be\u06c1\u06d5\u06f5\u07c0\u07cb"
           "\u0840\u0966\u09e6\u0a66\u0ae6\u0b20\u0b66\u0be6\u0c02\u0c66\u0c82\u0ce6\u0d02\u0d20\u0d66\u0d82\u0e50"
           "\u0ed0\u101d\u1040\u10ff\u110b\u11bc\u12d0\u17e0\u1a45\u1a80\u1a90\u1bea\u1c82\u1cbf\u1d0f\u1d11\u2c9e"
           "\u2c9f\u2d54\u3007\u3147\ua4f3\uab3d\ufba6\ufba7\ufba8\ufba9\ufbaa\ufbab\ufbac\ufbad\ufee9\ufeea\ufeeb"
           "\ufeec\uff10\uffb7\U00010292\U000102ab\U0001030f\U00010404\U0001042c\U000104c2\U000104ea\U00010516"
           "\U0001092c\U00010c17\U00010d07\U00011124\U00011302\U000114d0\U000118b5\U000118c8\U000118d7\U000118e0"
           "\U00011de0\U00016ae9\U0001cce4\U0001ccf0\U0001d6b6\U0001d6d0\U0001d6d4\U0001d6f0\U0001d70a\U0001d70e"
           "\U0001d72a\U0001d744\U0001d748\U0001d764\U0001d77e\U0001d782\U0001d79e\U0001d7b8\U0001d7bc\U0001d7ce"
           "\U0001d7d8\U0001d7e2\U0001d7ec\U0001d7f6\U0001e140\U0001e2f0\U0001ee24\U0001ee84\U0001fbf0",
    "p": "\u00fe\u01bf\u03a1\u03c1\u03f1\u03f8\u0420\u0440\u13e2\u146d\u2374\u2ca2\u2ca3\u2cce\u2ccf\ua4d1\U00010295"
           "\U0001cce5\U0001d6b8\U0001d6d2\U0001d6e0\U0001d6f2\U0001d70c\U0001d71a\U0001d72c\U0001d746\U0001d754"
           "\U0001d766\U0001d780\U0001d78e\U0001d7a0\U0001d7ba\U0001d7c8",
    "q": "\u051a\u051b\u0563\u0566\u2d55\U0001cce6",
    "r": "\u01a6\u024c\u0433\u13a1\u13d2\u1587\u1d26\u2c85\ua4e3\uab47\uab48\uab81\U000104b4\U00016a19\U00016f35"
           "\U0001cce7\U0001d216",
    "s": "\u01bd\u0405\u0455\u054f\u0d1f\u10bd\u10fd\u13d5\u13da\u1cbd\ua4e2\ua576\ua731\uabaa\U00010296\U00010420"
           "\U00010448\U000118c1\U00016ad6\U00016f3a\U0001cce8",
    "t": "\u03a4\u0422\u07e0\u13a2\u22a4\u27d9\u2ca6\u3112\u4e05\ua4d4\ua50b\U00010297\U000102b1\U00010315\U000118bc"
           "\U00016f0a\U0001cce9\U0001d373\U0001d6bb\U0001d6f5\U0001d72f\U0001d769\U0001d7a3\U0001f768",
    "u": "\u028b\u03c5\u054d\u057d\u1200\u144c\u1d1c\u222a\u22c3\ua4f4\ua79f\uab4e\uab52\U000104ce\U000104f6"
           "\U000118b8\U000118d8\U00016f42\U0001ccea\U0001d6d6\U0001d710\U0001d74a\U0001d784\U0001d7be",
    "v": "\u03bd\u0474\u0475\u05d8\u0667\u06f7\u13d9\u142f\u1d20\u2228\u22c1\u2d38\ua4e6\ua6df\uaba9\U0001051d"
           "\U00010c1f\U00011706\U000118a0\U000118c0\U00016f08\U0001cceb\U0001cefc\U0001d20d\U0001d6ce\U0001d708"
           "\U0001d742\U0001d77c\U0001d7b6\U0001e145",
    "w": "\u026f\u0448\u0461\u051c\u051d\u0561\u13b3\u13d4\u1d21\u29e2\u2cbd\ua4ea\ua7fa\uab83\uaba4\U0001170a"
           "\U0001170e\U0001170f\U000118e6\U000118ef\U0001ccec\U0001d20c\U0001df7d",
    "x": "\u00d7\u03a7\u0425\u0445\u1541\u157d\u166d\u166e\u16b7\u1763\u1bec\u1cf5\u2573\u292b\u292c\u2a2f\u2cac"
           "\u2d5d\ua4eb\ua7b3\U00010290\U000102b4\U00010317\U00010322\U00010527\U00010c13\U00010c82\U00010cc2"
           "\U00010cfc\U000118ec\U0001cced\U0001d6be\U0001d6f8\U0001d732\U0001d76c\U0001d7a6",
    "y": "\u0263\u028f\u03a5\u03b3\u03d2\u0423\u0443\u04ae\u04af\u07cc\u10e7\u13a9\u13bd\u1d8c\u1eff\u213d\u2ca8"
           "\u2ca9\u311a\u4e2b\ua4ec\uab5a\U000102b2\U00010c20\U000118a4\U000118c4\U000118dc\U00016f43\U0001ccee"
           "\U0001d6bc\U0001d6c4\U0001d6f6\U0001d6fe\U0001d730\U0001d738\U0001d76a\U0001d772\U0001d7a4\U0001d7ac",
    "z": "\u0396\u10cd\u13c3\u1d22\u2c6b\u2c6c\u2c8c\u2c8d\u2d2d\ua4dc\ua6c9\uab93\U000102f5\U00010507\U000118a9"
           "\U000118e5\U00011abc\U0001ccef\U0001d6ad\U0001d6e7\U0001d721\U0001d75b\U0001d795",
}

#: Code point -> the ASCII character it looks like.
LOOKALIKE: Dict[str, str] = {c: t for t, cs in _BY_TARGET.items() for c in cs}

SOURCE_VERSION = "18.0.0"

__all__ = ["LOOKALIKE", "SOURCE_VERSION"]
