# Customer brief: Northwind Outfitters (simulated)

## Who they are
A mid-market outdoor-gear retailer: direct-to-consumer web store, ~2M orders a year, a
40-person support team on Zendesk, orders in Shopify, customers in a homegrown CRM. Support
runs on email first; chat and phone are secondary.

## The business pain
* **Volume and cost.** ~1.4M support emails a year, roughly 60% of them one of five
  routine asks: where is my order, return or refund, damaged in transit, can't log in,
  sizing. Fully loaded cost per handled email is about $4.10. That is ~$3.4M a year on
  emails a policy document could answer.
* **Speed.** First-response time averages 11 hours; it is 26 hours during the holiday
  peak. Their own data shows refund-related churn rises sharply past 24 hours.
* **Consistency.** 40 agents apply the returns policy 40 ways. Goodwill refunds outside
  policy ran to $610k last year, most of it unreviewed.
* **Risk.** Two incidents last year: an agent quoted a warranty term that didn't exist, and a
  customer's card number was pasted into an internal Slack channel from a ticket.

(These figures are illustrative for the simulation; the shape is what matters.)

## Who cares, and about what
| Stakeholder | What they need to hear |
|---|---|
| **VP Customer Experience** (economic buyer) | cost per ticket, first-response time, CSAT; can I show my CFO a number in 90 days? |
| **CTO** (technical sponsor) | will this embarrass us? what runs where, what can it change, who approves, how do we roll it back |
| **Head of Support Ops** (day-to-day owner) | the approval queue is *their* tool; edits must flow back into quality; agents must not feel replaced on day one |
| **Security / Privacy** | PII never leaves our boundary in clear text; audit trail; data retention on the model side |
| **Support engineers** (build and own it) | typed contracts, tests, evals they can extend, no magic, on-call story |

## What "yes" looks like for them
A 90-day pilot on the five routine categories, measured against a held-out control group of
agents, with a kill switch and every auto-sent reply reviewable after the fact.

## What I rebuilt to demo it
I rebuilt their stack in miniature: **CRM** (customers, tiers, notes), **orders** (status,
tracking, delivery dates, final-sale flags), **tickets** (the store, statuses, audit trail),
**email** (IMAP in, SMTP out, .eml parsing), and their **policy knowledge base** (returns,
shipping, warranty, account, escalation SOP). Everything the agent reads or writes in the
demo goes through the same interfaces the production integration would.
