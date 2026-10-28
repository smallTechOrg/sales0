"""A hand-labelled set of SYNTHETIC replies (fictional people/companies, ``*.example`` domains).

Every case is what Gmail's metadata API would give us for an inbound message in the thread of a
cold email from smallTech: sender, subject, a ~150-200 character snippet and (sometimes) headers.
``expected_by == "headers"`` marks cases that must be decided without any LLM call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sales0.integrations.gmail.threads import ThreadMessage


@dataclass(frozen=True)
class Case:
    id: str
    from_addr: str
    subject: str
    snippet: str
    kind: str
    intent: str | None = None  # only for kind == "human"
    by: str = "llm"  # "headers" when the headers alone must decide
    from_name: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def message(self) -> ThreadMessage:
        extra: dict[str, Any] = dict(self.headers)
        return ThreadMessage(
            id=f"lab-{self.id}", thread_id="thr-lab", from_addr=self.from_addr.lower(), from_name=self.from_name,
            to_addrs=("ann@smalltech.example",), cc_addrs=(), subject=self.subject, date=datetime(2026, 10, 27, 9, 30, tzinfo=UTC),
            snippet=self.snippet, labels=("INBOX", "UNREAD"), **extra,
        )


RE = "Re: AI workshop for your managers"


def H(id: str, from_addr: str, subject: str, snippet: str, kind: str, from_name: str = "", **headers: str) -> Case:
    return Case(id, from_addr, subject, snippet, kind, None, "headers", from_name, headers)


def L(id: str, from_addr: str, snippet: str, kind: str, intent: str | None = None, subject: str = RE, from_name: str = "") -> Case:
    return Case(id, from_addr, subject, snippet, kind, intent if kind == "human" else None, "llm", from_name, {})


CASES: list[Case] = [
    # ------------------------------------------------------------------ decided by headers (no LLM)
    H("h-dsn-gmail", "mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)",
      "Address not found. Your message wasn't delivered to ravi.k@northwind.example because the address couldn't be found, or is unable to receive email.", "bounce", "Mail Delivery Subsystem"),
    H("h-postmaster-subject-only", "postmaster@brightpath.example", "Undelivered Mail Returned to Sender", "", "bounce"),
    H("h-multipart-report", "bounces@mx7.example", "Returned mail: see transcript for details",
      "This is the mail system at host mx7.example. I'm sorry to have to inform you that your message could not be delivered to one or more recipients.", "bounce",
      content_type="multipart/report; report-type=delivery-status; boundary=\"a1b2c3\""),
    H("h-x-failed", "noc@relay.example", "Mail delivery failed: returning message to sender",
      "Message could not be delivered to the following recipients. 550 5.1.1 <sanjay@harbor-foods.example>: Recipient address rejected: User unknown", "bounce",
      x_failed_recipients="sanjay@harbor-foods.example"),
    H("h-soft-bounce", "mailer-daemon@googlemail.com", "Delivery Status Notification (Delay)",
      "Delivery incomplete. There was a temporary problem delivering your message to dev@orchid-labs.example. Gmail will keep trying for 47 more hours.", "bounce", "Mail Delivery Subsystem"),
    H("h-ooo-auto-submitted", "meera.nair@lotuspay.example", "Re: AI workshop for your managers",
      "Hi, I am out of office until 4 Nov with limited access to email. For anything urgent please contact Rohan Iyer (rohan@lotuspay.example). Regards, Meera", "auto_reply",
      auto_submitted="auto-replied"),
    H("h-x-autoreply", "karan@greenfield-tech.example", "Re: AI workshop for your managers",
      "Thank you for your email. I am currently travelling and will respond to your message as soon as I return.", "auto_reply", x_autoreply="yes"),
    H("h-precedence-bulk", "newsletter@vendorhub.example", "Re: AI workshop for your managers",
      "Thanks for getting in touch with VendorHub. Your message has been logged. You can browse our supplier directory at any time.", "auto_reply", precedence="bulk"),
    H("h-outlook-automatic", "d.fernandes@coastline-retail.example", "Automatic reply: AI workshop for your managers",
      "I am on annual leave from 27 October to 7 November and will have no access to email. I will reply when I am back.", "auto_reply"),
    H("h-noreply-ack", "noreply@helpdesk.skyline-ins.example", "We received your message",
      "Thank you for contacting Skyline Insurance. Your request has been received and a ticket has been created. Please do not reply to this email.", "auto_reply"),
    H("h-ooo-subject", "priyanka.s@veritas-health.example", "Out of Office: AI workshop for your managers",
      "I'm away from the office and will be back on Monday 10 Nov. If your matter is urgent, please call our front desk.", "auto_reply"),
    # ------------------------------------------------------------------ bounces without strong headers (LLM)
    L("b-system-admin", "system-admin@oakridge-motors.example",
      "Your message to akash.v@oakridge-motors.example couldn't be delivered. The recipient's mailbox doesn't exist or the address is misspelled.", "bounce",
      subject="Undeliverable: AI workshop for your managers"),
    L("b-mail-delivery", "mail-delivery@campus-mail.example",
      "Delivery to the following recipient failed permanently: nisha.p@campus-mail.example. The response from the remote server was: 550 5.1.1 User unknown", "bounce",
      subject="Mail delivery failed"),
    # ------------------------------------------------------------------ auto replies without strong headers (LLM)
    L("a-ooo-contact", "rahul.mehta@tandem-logistics.example",
      "Thank you for your message. I am out of the office until 4 November with limited access to email. For urgent matters please contact Rohan Iyer at rohan@tandem-logistics.example.", "auto_reply"),
    L("a-outlook-snippet", "s.kulkarni@meridian-bank.example",
      "Automatic reply: Thank you for your email. I am currently on leave and will respond after my return on 10 Nov. For assistance, please reach out to HR operations.", "auto_reply"),
    L("a-ticket-ack", "support@cloudnest.example",
      "Thanks for contacting CloudNest Support. Your request #48213 has been received and a member of our team will respond within 2 business days.", "auto_reply",
      subject="[Ticket #48213] AI workshop for your managers"),
    L("a-not-monitored", "info@riverstone-estates.example",
      "This mailbox is not monitored. For sales enquiries please fill in the form on our website or call the number listed there.", "auto_reply"),
    L("a-hinglish-ooo", "vikram.joshi@sundaram-textiles.example",
      "Main 3 November tak chhutti par hoon aur email check nahi kar paunga. I will respond to your mail once I am back in office. Dhanyavaad.", "auto_reply"),
    L("a-receipt", "admin@silverline-edu.example",
      "This is an automated confirmation that your email was received by the admissions office. Please allow 3-5 working days. Do not reply to this message.", "auto_reply",
      subject="Thank you for contacting Silverline"),
    # ------------------------------------------------------------------ humans: interested
    L("i-call", "meera.nair@lotuspay.example",
      "Hi Ann, this looks relevant for us. Can we set up a call next Tuesday to discuss a workshop for our sales team of about 40? Regards, Meera", "human", "interested"),
    L("i-deck-pricing", "arjun.rao@bluepeak-energy.example",
      "Thanks for reaching out. Please send across the deck and pricing for the 2-day program. We have around 30 managers who would attend.", "human", "interested"),
    L("i-hinglish", "kavita.shah@sundaram-textiles.example",
      "Namaste Ann ji, aapka workshop idea accha hai. Can you share the proposal aur schedule? Hum next month mein plan kar rahe hain.", "human", "interested"),
    L("i-timing", "tom.becker@northwind.example",
      "Interesting timing - we were just discussing GenAI training for the ops team. Are you free for a quick call Thursday at 3pm?", "human", "interested"),
    L("i-references", "latha.iyer@pioneer-pharma.example",
      "Yes, we are interested. Could you share a sample agenda and a couple of client references from the pharma sector? We would like to decide by month end.", "human", "interested"),
    L("i-short", "dev.malhotra@zenith-cars.example", "Sounds good, let's talk. Call me anytime this week.", "human", "interested"),
    # ------------------------------------------------------------------ humans: question
    L("q-who", "gita.krishnan@harbor-foods.example",
      "Who are you and how did you get my email address? I don't recall asking for anything like this.", "human", "question"),
    L("q-content", "paul.dsouza@orchid-labs.example",
      "Can you tell me more about what exactly the workshop covers? Is it hands-on or lecture based, and do participants need a coding background?", "human", "question"),
    L("q-audience", "nina.thomas@brightpath.example",
      "Is this meant for engineers or non-technical staff? And what is your typical group size and duration?", "human", "question"),
    L("q-clients", "sameer.ali@greenfield-tech.example",
      "Which companies in Pune have you worked with so far? Also do you run these in person only?", "human", "question"),
    L("q-who-is-this", "anita.bose@coastline-retail.example",
      "Sorry, who is this? I don't recall speaking to anyone from smallTech. What is this regarding?", "human", "question"),
    # ------------------------------------------------------------------ humans: not now
    L("n-budget", "farhan.sheikh@veritas-health.example",
      "Thanks for the note, but we've frozen training budgets until the next financial year. Please reach out again in April.", "human", "not_now"),
    L("n-polite-decline", "claire.dupont@skyline-ins.example",
      "Thank you for thinking of us. We're not looking at anything like this right now, but I wish you the best with your work.", "human", "not_now"),
    L("n-vendor", "harish.rao@oakridge-motors.example",
      "Appreciate you writing. We already have a training vendor under contract through 2027, so we will pass for now.", "human", "not_now"),
    L("n-quarter", "isha.verma@meridian-bank.example", "Not a priority this quarter, we are tied up with the audit. Maybe revisit in Q1?", "human", "not_now"),
    L("n-diwali", "manoj.pillai@tandem-logistics.example",
      "Thanks for the follow up. The team is swamped until Diwali, can we reconnect in the second week of November?", "human", "not_now"),
    L("n-hiring-freeze", "olivia.grant@cloudnest.example",
      "Hi Ann, bad timing for us - there is a hiring and spending freeze in place at the moment. Thanks for understanding.", "human", "not_now"),
    # ------------------------------------------------------------------ humans: unsubscribe
    L("u-remove", "ben.carter@riverstone-estates.example", "Please remove me from your list.", "human", "unsubscribe"),
    L("u-angry", "ritu.sen@silverline-edu.example",
      "STOP emailing me. This is spam and I will report it to my IT team if I get one more of these.", "human", "unsubscribe"),
    L("u-word", "lars.nielsen@lotuspay.example", "Unsubscribe", "human", "unsubscribe"),
    L("u-dpdp", "usha.menon@bluepeak-energy.example",
      "Do not contact me again. How did you get my details? I am considering a complaint under the data protection law.", "human", "unsubscribe"),
    L("u-decline-and-stop", "yash.kapoor@northwind.example",
      "Not interested. Please take me off your mailing list and don't email this address again.", "human", "unsubscribe"),
    # ------------------------------------------------------------------ humans: other (forwarding, wrong person, bounced-to-me)
    L("o-forward", "rahul.sharma@pioneer-pharma.example",
      "Forwarding this to our HR lead, Sunita, who handles training vendors. Sunita, please have a look when you get time. - Rahul", "human", "other"),
    L("o-wrong-person", "zoe.walsh@harbor-foods.example",
      "I think you have the wrong person. I left the company in March; I'm no longer the L&D manager here.", "human", "other"),
    L("o-noted", "dinesh.kumar@zenith-cars.example", "Got it, thank you.", "human", "other"),
    L("o-bounced-to-me", "anil.gupta@orchid-labs.example",
      "Hi, your mail bounced back to me and I saw it in my spam folder instead. Please resend it to this address if it was meant for me.", "human", "other",
      subject="Re: AI workshop for your managers"),
    L("o-colleague-cc", "preeti.nambiar@brightpath.example",
      "Adding my colleague Jay here, he looks after team development. Jay, can you take this?", "human", "other"),
]
