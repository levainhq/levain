# Web push: what a notification can carry, and how a tap is followed

This page records two facts about the cockpit's web push that are easy to get wrong from memory. Claims about code
were checked against the source when the page was written; claims about iOS come from the research note
`project_memory/1106-16/RESEARCH_notif_actions.md` (2026-11-06), which labels each finding as read, snippet or absent.

## A notification carries no buttons on iOS

Safari's compatibility data lists `showNotification`'s `options.actions` as unsupported, and iOS mirrors it; Apple's
forum reports from 2023 to late 2024 show a default "View" and ignored custom buttons. Declarative web push has a
`navigate` URL a tap opens and no actions field. The note's finding that nothing lets a web page tell a lock-screen
view from a Notification Center view is an absence of evidence, not a proof.

So the design is: **a tap opens the card, and the answer is signed on the card.** A notification never asks for a
decision itself. Native buttons would need a native app, a separate decision (the research note's recommendation was to
keep the tap-opens-the-card shape and harden it). `/devtest` is the one place a notification asks for `actions`, as a
measurement of what a given device and iOS version does with them; its results are read with `levain ping devtest`.

How the tap is followed (so a wrong landing is visible rather than silent): the service worker reports the click and
how the window was opened, the page reports where it landed, and a resume handler covers a tap iOS delivered without
the click event. The Needs-you link then resolves the item through the band read. All of it is recorded
server-side per device (`levain ping` shows it).

## The sender's proxy environment is read once, at import

`levain/webpush/sender.py` builds its URL opener at module import (`urllib.request.build_opener`). Python's opener
reads the proxy environment variables (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` and their lower-case forms) when it is
built, so a proxy variable set or changed in the process after `levain.webpush.sender` was imported is not seen by
pushes. This is current behaviour, and the decision is to keep it: the process environment a cockpit starts with is
the one its pushes use. To change the proxy, set it before starting the process.
