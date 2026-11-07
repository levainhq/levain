# Web push: what a notification can carry, and how a tap is followed

This page records facts about the cockpit's web push that are easy to get wrong from memory. Claims about iOS come from
a research note written on 2026-11-06, `project_memory/1106-16/RESEARCH_notif_actions.md`. That note is local to the
machine it was written on: `project_memory/` is ignored by git, so it is not in a clone of this repository. The sources
it rests on are listed under "Sources for the iOS claims" below, so a reader can follow them without the note.

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
server-side per device (`levain ping receipts` shows it).

## The sender's proxy environment is read once, at import

`levain/webpush/sender.py` builds its URL opener at module import (`urllib.request.build_opener`). Python's opener
captures the proxy mapping (`HTTP_PROXY`, `HTTPS_PROXY` and their lower-case forms) when it is built, so a change to
one of those variables in the process after `levain.webpush.sender` was imported is not seen by pushes. `NO_PROXY` is
different: `urllib` consults it for each request, so a change to it after import does take effect. The decision is to
keep the import-time capture of the proxy mapping: the process environment a cockpit starts with is the one its pushes
use. To change the proxy, set it before starting the process.

## Sources for the iOS claims

Read on 2026-11-06 (the research note labels each as read or as a search snippet only):

- Safari and iOS Safari support for `options.actions`, `Notification.maxActions` and `options.navigate`: the
  `mdn/browser-compat-data` repository, `api/ServiceWorkerRegistration.json` and `api/Notification.json`.
- Apple developer forum thread 726793, "Does Web Push Notification Actions work on iOS 16.4?" (March 2023 onward, last
  reply November 2024): user reports only, no answer from Apple staff.
- WebKit blog post 13878, "Web Push for Web Apps on iOS 16.4" (`webkit.org/blog/13878`): notifications show on
  the Lock Screen, in Notification Center and on a paired Apple Watch; it does not mention actions.
- WebKit blog post 16535, "Declarative Web Push" (`webkit.org/blog/16535`): the payload fields, `navigate` among them,
  and no actions field.
- Tap-landing reports the tap-following design answers: WebKit bug 263687, Apple developer forum thread 733604 and
  Discourse topic 255975, which disagree about which app state (running or closed) fails to navigate.

Search snippets only: WebKit commits that added `defaultAction` for declarative push and reworked notification-click
routing, none of which mention action buttons. No WebKit bug or release note adding `actions` was found, which is an
absence of evidence in a search and not proof; `/devtest` exists to measure it on a device.
