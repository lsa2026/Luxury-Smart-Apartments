"""Versioned guest documents: one source for display and consent evidence.

Never edit an issued version in place. Bump VERSION and retain old guest
snapshots when issuing a replacement. These are contractual terms, not a
certification of licences or of compliance in either jurisdiction.
"""

# ruff: noqa: E501 -- Keep the issued multilingual copy as complete text literals.

import hashlib
import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from django.utils.translation import get_language

VERSION = "2026-10-05.1"

TERMS_TITLES = {
    "ar": "شروط الحجز والإقامة وإشعار الخصوصية",
    "en": "Booking and stay conditions and privacy notice",
    "fr": "Conditions de réservation et de séjour et information sur les données personnelles",
}

PRIVACY_TITLES = {
    "ar": "إشعار الخصوصية",
    "en": "Privacy notice",
    "fr": "Information sur les données personnelles",
}

UI = {
    "ar": {
        "version": "الإصدار",
        "issued": "تاريخ الإصدار: ٥ أكتوبر ٢٠٢٦",
        "scope": "تطبق هذه النسخة على الحجوزات الجديدة التي توافق عليها بعد نشرها، لا بأثر رجعي.",
        "terms_acceptance": "قرأت ووافقت على شروط الحجز والإقامة وشروط السعر والإلغاء المعروضة لهذا الحجز، واطلعت على إشعار الخصوصية.",
        "read": "قراءة الوثيقة كاملة",
        "saved": "الوثيقتان اللتان وافقت عليهما",
        "accepted_at": "تاريخ ووقت الموافقة",
        "updated": "تغيرت الوثائق منذ فتح الصفحة. أعد تحميل الصفحة، ثم اقرأ النسخة الحالية ووافق مجددًا قبل الدفع.",
    },
    "en": {
        "version": "Version",
        "issued": "Issued: 5 October 2026",
        "scope": "This version applies to new bookings accepted after publication, not retrospectively.",
        "terms_acceptance": "I have read and accept the booking and stay conditions and the rate and cancellation conditions shown for this booking, and have read the privacy notice.",
        "read": "Read the full document",
        "saved": "The two documents you accepted",
        "accepted_at": "Acceptance date and time",
        "updated": "The documents have changed since you opened this page. Reload the page, then read and accept the current version before paying.",
    },
    "fr": {
        "version": "Version",
        "issued": "Émis le 5 octobre 2026",
        "scope": "Cette version s’applique aux nouvelles réservations acceptées après sa publication, sans effet rétroactif.",
        "terms_acceptance": "J’ai lu et j’accepte les conditions de réservation et de séjour ainsi que les conditions tarifaires et d’annulation présentées pour cette réservation, et j’ai pris connaissance de l’information sur les données personnelles.",
        "read": "Lire le document intégral",
        "saved": "Les deux documents que vous avez acceptés",
        "accepted_at": "Date et heure d’acceptation",
        "updated": "Les documents ont changé depuis l’ouverture de cette page. Rechargez la page, puis lisez et acceptez la version actuelle avant de payer.",
    },
}

HOUSE_RULES = {
    "ar": {
        "title": "تعليمات المنزل",
        "intro": "حرصًا على راحتكم وراحة الجيران والمحافظة على الشقة، يرجى الالتزام بالتعليمات التالية. تنطبق على جميع شققنا، بما فيها شقة مراكش.",
        "items": [
            "ممنوع التدخين داخل الشقة بجميع أنواعه، بما فيها الشيشة والسجائر الإلكترونية. فتح النوافذ أو تشغيل الشفاط لا يجعل التدخين مسموحًا. ولا يُسمح به في الشرفات أو المناطق المشتركة إلا إذا أُعلن صراحة عن مكان مخصص.",
            "ممنوع إقامة الحفلات والتجمعات المزعجة أو رفع الموسيقى وإصدار ضوضاء تؤذي الجيران، في أي وقت.",
            "تقتصر الإقامة على عدد الضيوف المعتمد في الحجز، ويُمنع تجاوز هذا العدد. يُمنع اصطحاب الحيوانات الأليفة إلى جميع الشقق، بما فيها شقة مراكش. وتُراعى ضوابط الزيارات المعروضة قبل الحجز.",
            "يرجى المحافظة على الأثاث والأجهزة، وعدم نقل ممتلكات الشقة خارجها، وإبلاغ الإدارة فورًا عن أي تلف أو خلل.",
            "يمنع العبث بكواشف الدخان وأجهزة السلامة والأقفال، أو إعاقة مخارج الطوارئ، أو استخدام الشقة في أنشطة مخالفة للقانون.",
            "الدخول من الساعة الثالثة عصرًا، والخروج بحلول الثانية عشرة ظهرًا، بالتوقيت المحلي للشقة. يسعدنا التعاون بشأن الدخول المبكر والخروج المتأخر حسب التوفر، بعد موافقة الإدارة.",
            "حافظوا على سرية رموز الدخول، واحترموا خصوصية السكان وتعليمات المبنى والمواقف المخصصة. ويرجى إطلاع جميع المرافقين على هذه التعليمات.",
            "قد تؤدي المخالفة المثبتة إلى المطالبة بتعويض عن الضرر الفعلي، أو إنهاء الإقامة وفق الإجراءات والقانون الواجب التطبيق. ولا تعني الموافقة تفويضًا مفتوحًا للخصم من البطاقة.",
        ],
        "acceptance": "قرأت تعليمات المنزل ووافقت على الالتزام بها وإطلاع مرافقيّ عليها، وأفهم منع التدخين والحفلات والإزعاج واصطحاب الحيوانات الأليفة.",
    },
    "en": {
        "title": "House rules",
        "intro": "For your comfort, our neighbours' peace and the care of the apartment, please follow these rules. They apply to all our apartments, including Marrakech.",
        "items": [
            "Smoking of any kind is prohibited inside the apartment, including shisha and electronic cigarettes. Opening windows or using an extractor does not make smoking permissible. Smoking on balconies or in shared areas is not permitted unless a designated smoking area is expressly identified.",
            "Parties, disruptive gatherings, loud music and noise that disturbs neighbours are prohibited at all times.",
            "Only the number of guests approved in the booking may stay; this number must not be exceeded. Pets are prohibited in all apartments, including Marrakech. Observe the visitor rules disclosed before booking.",
            "Take care of furniture and appliances, do not remove apartment belongings, and report any damage or fault to management promptly.",
            "Do not tamper with smoke detectors, safety equipment or locks, obstruct emergency exits, or use the apartment for unlawful activities.",
            "Check-in is from 3:00 p.m. and check-out is by 12:00 noon, in the apartment's local time. We are happy to help with early check-in or late check-out subject to availability and management approval.",
            "Keep access codes confidential and respect residents' privacy, building rules and assigned parking. Share these rules with everyone accompanying you.",
            "A proven breach may lead to a claim for actual damage or termination of the stay through lawful procedures and under applicable law. Acceptance does not authorise unrestricted charges to your card.",
        ],
        "acceptance": "I have read and agree to follow the house rules and share them with my companions. I understand that smoking, parties, disruptive noise and pets are prohibited.",
    },
    "fr": {
        "title": "Règlement intérieur",
        "intro": "Pour votre confort, la tranquillité du voisinage et la préservation de l’appartement, veuillez respecter ces règles. Elles s’appliquent à tous nos appartements, y compris celui de Marrakech.",
        "items": [
            "Il est interdit de fumer à l’intérieur de l’appartement, sous quelque forme que ce soit, y compris la chicha et les cigarettes électroniques. Ouvrir les fenêtres ou utiliser une hotte ne rend pas le tabagisme autorisé. Il est également interdit de fumer sur les balcons ou dans les parties communes, sauf dans un espace expressément désigné à cet effet.",
            "Les fêtes, rassemblements perturbateurs, la musique forte et les nuisances sonores gênant les voisins sont interdits à toute heure.",
            "Le séjour est limité au nombre de voyageurs approuvé dans la réservation, sans dépassement. Les animaux de compagnie sont interdits dans tous les appartements, y compris celui de Marrakech. Respectez les règles relatives aux visiteurs présentées avant la réservation.",
            "Prenez soin du mobilier et des appareils, ne sortez pas les biens de l’appartement et signalez rapidement tout dommage ou dysfonctionnement à la direction.",
            "Ne manipulez pas les détecteurs de fumée, les équipements de sécurité ou les serrures, ne bloquez pas les issues de secours et n’utilisez pas l’appartement pour des activités illégales.",
            "L’arrivée est possible à partir de 15 h et le départ doit avoir lieu au plus tard à midi, à l’heure locale de l’appartement. Nous pouvons faciliter une arrivée anticipée ou un départ tardif, selon les disponibilités et après accord de la direction.",
            "Gardez les codes d’accès confidentiels et respectez la vie privée des résidents, le règlement de l’immeuble et les places de stationnement attribuées. Informez toutes les personnes qui vous accompagnent de ces règles.",
            "Un manquement établi peut entraîner une demande d’indemnisation du préjudice réel ou la résiliation du séjour selon les procédures légales et le droit applicable. L’acceptation n’autorise pas des prélèvements illimités sur votre carte.",
        ],
        "acceptance": "J’ai lu et j’accepte de respecter le règlement intérieur et d’en informer les personnes qui m’accompagnent. Je comprends que le tabagisme, les fêtes, les nuisances sonores et les animaux de compagnie sont interdits.",
    },
}


def document_language(language: str | None = None) -> str:
    code = (language or get_language() or "ar").split("-")[0]
    return code if code in HOUSE_RULES else "ar"


def house_rules_document(language: str | None = None) -> dict:
    code = document_language(language)
    document = deepcopy(HOUSE_RULES[code])
    document.update(id="LSA-HOUSE-RULES", version=VERSION, language=code)
    document["body"] = (
        document["intro"]
        + "\n\n"
        + "\n\n".join(f"{index}. {item}" for index, item in enumerate(document["items"], 1))
    )
    document["sha256"] = hashlib.sha256(document["body"].encode("utf-8")).hexdigest()
    return document


def document_digest(documents: dict) -> str:
    return hashlib.sha256(
        json.dumps(documents, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


@lru_cache(maxsize=3)
def _terms_body(code: str) -> str:
    return (
        (Path(__file__).parent / "guest_terms" / f"{code}.txt").read_text(encoding="utf-8").strip()
    )


def guest_documents(language: str | None = None) -> dict:
    code = document_language(language)
    body = _terms_body(code)
    return {
        "terms": {
            "id": "LSA-GUEST-TERMS",
            "version": VERSION,
            "language": code,
            "title": TERMS_TITLES[code],
            "body": body,
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "acceptance": UI[code]["terms_acceptance"],
        },
        "house_rules": house_rules_document(code),
    }


def documents_digest() -> str:
    """Bind the form to the complete multilingual issued document set."""
    return document_digest({code: guest_documents(code) for code in HOUSE_RULES})
