"""Fail the Docker BUILD if the dependency set is subtly wrong.

Two failures already reached us the slow way, so they are asserted here:

1. httpx must stay on 0.27.x. python-telegram-bot 21.6 pins httpx~=0.27 and
   openai 1.54 raises "Client.__init__() got an unexpected keyword argument
   'proxies'" on 0.28 - which kills every single reply, at runtime, with a
   green build.

2. ddgs is installed with --no-deps (its declared httpx>=0.28.1 is impossible
   here), so its transitive imports have to be pinned by hand in
   requirements.txt. `from ddgs import DDGS` is a LAZY stub that imports
   nothing, so it must actually be CONSTRUCTED to load the search engines -
   that is the only way a missing h2 shows up at build time instead of the
   first time a user asks the bot to look something up.

Kept as a file rather than an inline `python -c` in the Dockerfile: a
multi-line -c string breaks the RUN line-continuation, and Docker then reads
"from openai import OpenAI" as a FROM instruction.

ASCII only - the build log mangles anything else.
"""
from __future__ import annotations

import sys


def main() -> int:
    import httpx
    import openai  # noqa: F401
    import rapidfuzz  # noqa: F401
    import trafilatura  # noqa: F401

    if not httpx.__version__.startswith("0.27"):
        print(f"FAIL: httpx is {httpx.__version__}, must stay 0.27.x - "
              "0.28 breaks openai 1.54 and python-telegram-bot 21.6",
              file=sys.stderr)
        return 1

    # Must construct, not just import - see the note above.
    from openai import OpenAI
    OpenAI(api_key="build-check")

    from ddgs import DDGS
    DDGS()

    import telegram  # noqa: F401  - the other httpx-sensitive package

    print(f"dependency check OK - httpx {httpx.__version__}, "
          f"openai {openai.__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
