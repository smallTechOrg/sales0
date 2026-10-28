"""Labelled synthetic posts for the triage prompt regression (agent.md -> Eval, Phase 1.4).

Every company, person, email and phone is fictional (``.example`` domains, 555-style
numbers). 16 genuine + 24 discard. A prompt change that drops verdict accuracy below 90 %
does not ship.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class EvalPost:
    id: str
    kind: str
    expected: Literal["genuine", "discard"]
    text: str
    author: str = "Sample Author"
    headline: str = "Sample headline"


def _g(id: str, kind: str, text: str, headline: str = "L&D Head") -> EvalPost:
    return EvalPost(id, kind, "genuine", text, headline=headline)


def _d(id: str, kind: str, text: str, headline: str = "Talent Acquisition") -> EvalPost:
    return EvalPost(id, kind, "discard", text, headline=headline)


EVAL_POSTS: list[EvalPost] = [
    # ------------------------------------------------------------------ genuine (16)
    _g("G01", "buyer_email", "Zenith Textiles is looking for a training partner for a 2-day Generative AI workshop for 35 plant managers in Coimbatore, offline, in December. Send proposals to learning@zenithtextiles.example."),
    _g("G02", "buyer_phone", "Our HR team at Maple Health Systems needs an AI productivity workshop for 80 nurses-administrators. Looking for a vendor, online preferred. Call Rohan on +91 99887 70011 or reach out here."),
    _g("G03", "no_contact", "We want all 120 engineers at Helio Payments to be trained on Claude Code and agentic coding this quarter. Can anyone recommend a solid corporate training provider?"),
    _g("G04", "build", "Quartz Insurance is looking for a team to design and build an AI assistant that answers customer questions from our policy PDFs. DM me to discuss scope and timelines.", "COO"),
    _g("G05", "institute", "Our institute, Riverbend College of Management, wants to run a faculty development programme on using GenAI in teaching for 45 faculty members in January. Seeking a training company. Email dean.office@riverbend.example."),
    _g("G06", "no_contact", "Looking for a company that can deliver an AI literacy workshop for our leadership team of about 25 people. Hybrid is fine. Any suggestions?", "VP People"),
    _g("G07", "emoji", "🚀 We're hiring a PARTNER (not a person!) 🤝 to train our 200-strong sales force on GenAI tools 🤖 in Pune next month. Companies with corporate experience please send proposals to  growth@brightkart.example 📩", "CHRO"),
    _g("G08", "hindi", "हमारी कंपनी सनराइज़ फूड्स को अपने मैनेजरों के लिए एक दिन की AI वर्कशॉप कराने के लिए ट्रेनिंग पार्टनर चाहिए। जयपुर में, ऑफलाइन। कृपया संपर्क करें: training@sunrisefoods.example", "HR Manager"),
    _g("G09", "hinglish", "Hamari company ko apni tech team ke liye LLM app development ki hands-on training chahiye. Koi good training company ho to please DM karein. 30 developers hain, Hyderabad.", "Engineering Manager"),
    _g("G10", "build", "We are a logistics startup evaluating vendors to build an AI agent that automates our invoice reconciliation. Open to proposals from AI consulting firms. Write to ops@fleetlane.example.", "Founder"),
    _g("G11", "product", "Looking for an off-the-shelf AI tool for meeting notes and action items for our 300-person consulting firm. Which vendors should we evaluate? Happy to take demos, ping me.", "CIO"),
    _g("G12", "buyer_email", "RFP: Northgate Bank invites training providers to propose a 3-day workshop on Responsible AI and GenAI use cases for 50 senior managers (Mumbai, on-site, Q1). Contact procurement@northgatebank.example by 30 Nov.", "Head of Learning"),
    _g("G13", "no_contact", "Our team at Cedar Analytics wants to get started with AI agents. We need a partner to run a hands-on build-your-first-agent bootcamp for 20 data engineers. Open to online delivery.", "Director Engineering"),
    _g("G14", "buyer_phone", "Urgent: need a trainer-company for a one-day prompt engineering workshop for our 40 support executives, Noida office, next week. Whatsapp 98100 55512 with your proposal and quote.", "Operations Head"),
    _g("G15", "consulting", "Considering AI consulting for our manufacturing plants (predictive maintenance + a shop-floor Q&A bot). Looking for a firm to run a discovery workshop with our leadership. Reach out via DM.", "Plant Head"),
    _g("G16", "no_contact", "Does anyone know a reliable partner who can train our 500 employees on GenAI basics in a blended format over the next two quarters? We are Lakeshore Retail's L&D team.", "L&D Lead"),
    # ------------------------------------------------------------------ discard (24)
    _d("D01", "agency", "Hiring! TrainHub is onboarding freelance AI trainers for our corporate clients across India. 5+ years experience. Share your profile and per-day commercials."),
    _d("D02", "agency", "Trainer requirement: GenAI for a leading bank client, Mumbai, 3 days, online. EduSpark Learning is empanelling subject-matter experts. Send your CV and daily rate to trainers@eduspark.example."),
    _d("D03", "marketplace", "We are looking for freelance Corporate Trainers (Agentic AI, Prompt Engineering, LLMOps) for upcoming batches of our enterprise clients. Remote, weekdays, competitive payout per session. Apply now: careers@learnbridge.example."),
    _d("D04", "marketplace", "URGENT requirement 📢 Freelance trainer needed for Generative AI - 5 day batch for our client in Bengaluru. Interested trainers please share profile + expected rate per day. Briltus Learning Partners.", "Associate, Briltus-style Edtech"),
    _d("D05", "agency", "Greetings from SkillForge Academy. We are expanding our trainer network for Data Science and AI. Experienced trainers can register on our portal and get assignments from top corporates."),
    _d("D06", "agency", "Looking for Azure AI and LLM trainers for a weekend corporate batch at one of our clients. Share your updated profile and commercials by Friday. Contact +91 90000 11122."),
    _d("D07", "seeker", "Looking for an individual trainer for a weekend Python + ChatGPT batch, hourly basis. Interested trainers DM me your rates."),
    _d("D08", "seeker", "Need a freelance AI trainer for my small team of 6, 2 hours daily for 5 days, budget Rs 500 per hour. Comment or DM if interested."),
    _d("D09", "trainer_ad", "10 years of corporate training, 400+ batches delivered. Available for freelance AI and Excel workshops. Open to work, ping me.", "Corporate Trainer"),
    _d("D10", "trainer_ad", "I help companies upskill their teams with hands-on GenAI workshops. Book a free discovery call at coach@aiwithmeera.example. Limited slots this month!", "AI Trainer & Speaker"),
    _d("D11", "trainer_ad", "After 8 years in L&D I am now open to freelance assignments in AI training and facilitation. Happy to connect with agencies and institutes.", "Freelance Facilitator"),
    _d("D12", "event", "Join our 2-day Agentic AI Bootcamp on 15 Nov! Limited seats, early-bird 30% off. Register at the link in comments.", "Founder, Edtech"),
    _d("D13", "event", "📣 FREE WEBINAR: How to build AI agents with Claude Code. Saturday 11 AM IST. 1,000+ already registered. Save your seat now!", "Community Lead"),
    _d("D14", "event", "Excited to announce AI Summit Bengaluru 2027! Speakers from 40+ companies, 2000 attendees. Early-bird tickets on sale. Sponsorship enquiries: sponsors@aisummit.example.", "Event Organiser"),
    _d("D15", "event", "Enrolments open for our 12-week Generative AI certification programme with placement assistance. Call +91 98765 00123 for a counselling session.", "Admissions"),
    _d("D16", "job", "Hiring: Senior AI Trainer (full-time, Hyderabad). 4+ years, CTC 18-24 LPA. Apply with your resume at careers@orbitlearn.example."),
    _d("D17", "job", "We are hiring a Machine Learning Engineer for our Pune office. Python, LLMs, RAG. Send CV to hr@datanest.example. Immediate joiners preferred."),
    _d("D18", "job", "Opening for Learning & Development Manager (AI upskilling) at a Fortune 500 company in Gurugram. Permanent role. DM your resume."),
    _d("D19", "thought", "Agentic AI training is going to be the most important skill of 2027. Here are 5 lessons I learned after 100 sessions. Thoughts?", "Author | Speaker"),
    _d("D20", "thought", "Everyone is talking about AI training for employees. But the real question: are leaders trained first? My take on why corporate AI adoption stalls. #AI #Leadership", "Management Consultant"),
    _d("D21", "celebration", "Thrilled to share that our team just trained its 10,000th learner on Generative AI! Thank you to all our clients and partners 🎉🙏", "CEO, Training Company"),
    _d("D22", "news", "OpenAI and Anthropic race ahead on agent frameworks. Enterprises are spending more on AI upskilling than ever, according to a new report. Read the full article here.", "Tech Journalist"),
    _d("D23", "outside", "Need SAP ABAP corporate trainer for a 10-day on-site batch in Chennai. Companies with SAP certified trainers only. Email sapteam@gearworks.example.", "SAP Practice Head"),
    _d("D24", "hindi_agency", "हमें अपने क्लाइंट के लिए फ्रीलांस AI ट्रेनर चाहिए, दिल्ली में 3 दिन की बैच। अपनी प्रोफ़ाइल और प्रति दिन की फीस शेयर करें।", "Training Coordinator"),
]

assert len(EVAL_POSTS) >= 40
assert len({p.id for p in EVAL_POSTS}) == len(EVAL_POSTS)
