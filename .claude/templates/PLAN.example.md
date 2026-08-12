# PLAN — <what you are building>

<!--
════════════════════════════════════════════════════════════════════════════
WHAT THIS FILE IS

This is the ONE thing the loop cannot work out for you: what you want built and
why. Everything else — which checks to run, how to split the work, what order to
build in — the harness figures out from here.

Write it in plain English. You are describing the product to a capable engineer
who has never seen it. You are NOT writing a spec, a ticket, or anything
technical. Rough is fine. Three paragraphs is fine.

WHAT HAPPENS WHEN YOU RUN IT

    ./rocket.sh plan PLAN.md

Three agents argue about this file — one for it, one against it, one on whether
it can actually be built — and a fourth settles the argument. Out of that come
DRAFT feature briefs in features/_drafts/ and a proposed build order.

Nothing is built from those drafts automatically. You read them, keep the ones
you want, and move them into FEATURE_QUEUE.md yourself. Then `./rocket.sh`
builds them one at a time.

HOW TO USE THIS FILE

Delete these comments and replace every <angle-bracket> below. Delete any
section you have nothing to say about — an empty section is better than a
guessed one, because the agents will argue from whatever you write here. A
sentence you made up to fill a gap becomes a feature nobody wanted.

There is a filled-in example at the bottom. Look at that first.
════════════════════════════════════════════════════════════════════════════
-->

## What this is

<Two or three sentences. What is the thing, and who uses it? Write it the way
you would say it out loud to a friend.>

## Why it needs to exist

<What is broken, missing, or painful right now? What does someone do TODAY
instead, and why is that bad? This is the section that decides whether a
proposed feature is worth building, so it does more work than it looks like.>

## What it has to do

<A list. One line each, no technical detail, no ordering. "A user can X."
Five to fifteen lines is a healthy size for one plan. If you have forty, this is
several plans — build the first one and come back.>

- <a user can ...>
- <a user can ...>
- <a user can ...>

## What it must NOT do

<Just as important, and almost always skipped. Anything you would be unhappy to
find built. Scope you are deliberately leaving out. If you leave this empty, the
agents will argue for the most ambitious reading of the section above.>

- <out of scope: ...>

## How you will know it works

<For each thing above, how would YOU check it by hand? "I can open the app, do
X, and see Y." This is what the reviewers are held to, so vagueness here is the
single most expensive thing in this file. "It should feel fast" is not
checkable; "the page finishes loading before I can count to two" is.>

## Anything already decided

<Optional. Constraints that are not up for debate: a language, a hosting
provider, a database you already pay for, a deadline, a design you have already
signed off. If it is genuinely open, leave it out and let the debate decide.>

---

<!--
════════════════════════════════════════════════════════════════════════════
EXAMPLE — delete everything below once you have written your own.
This is the level of detail that works. It is not technical anywhere.
════════════════════════════════════════════════════════════════════════════

# PLAN — Invoice chaser

## What this is

A small web app for freelancers who are owed money. It reads your unpaid
invoices, and when one goes past due it drafts a polite chase email you can send
with one click.

## Why it needs to exist

Right now I check my accounting software every couple of weeks, notice something
is 40 days overdue, and then spend ten minutes writing an awkward email. Half the
time I put it off and it slips another month. The chasing is not hard, it is just
unpleasant enough that it does not happen.

## What it has to do

- A user can connect their accounting account and see every unpaid invoice.
- A user can see, at a glance, which invoices are overdue and by how long.
- A user can see a drafted chase email for any overdue invoice.
- A user can edit that draft before sending.
- A user can send it, and see that it was sent and when.
- A user can see the full history of what was chased and when.

## What it must NOT do

- Out of scope: sending anything automatically. Every email is sent by a human
  pressing a button. Getting this wrong damages a real client relationship.
- Out of scope: payments. This app never touches money.
- Out of scope: multiple users or teams. One person, one account.

## How you will know it works

- I can connect my account and the invoice list matches what I see in my
  accounting software, including the amounts.
- An invoice due last week shows as overdue by 7 days.
- An invoice due tomorrow does not show as overdue at all.
- I can open a draft, and it names the client, the invoice number and the
  amount correctly — not placeholders.
- I can change a word in the draft, send it, and the email that arrives in my
  own inbox contains my change.
- After sending, the invoice shows "chased today" and does not offer to chase
  again the same day.

## Anything already decided

- It has to work with the accounting software I already pay for.
- One screen. If it needs a second screen, it has grown past what I want.
════════════════════════════════════════════════════════════════════════════
-->
