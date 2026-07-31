from django.db import migrations


PAGES = {
    "about": {
        "title_fr": "À propos",
        "body_fr": (
            "Luxury Smart Apartments est une plateforme locale dédiée aux séjours "
            "quotidiens et mensuels. Nous privilégions des informations claires sur "
            "les logements ainsi qu’une vérification en direct du prix et des "
            "disponibilités avant la poursuite d’une demande de réservation.\n\n"
            "Cette page peut être mise à jour depuis l’espace d’administration "
            "lorsque le contenu définitif de la marque est approuvé."
        ),
    },
    "terms": {
        "title_fr": "Conditions générales",
        "body_fr": (
            "Ceci est une version initiale soumise à une révision juridique. Un "
            "devis est temporaire et une demande de réservation locale ne constitue "
            "pas une réservation confirmée. La confirmation nécessite l’achèvement "
            "des étapes opérationnelles qui seront approuvées ultérieurement."
        ),
    },
    "privacy": {
        "title_fr": "Politique de confidentialité",
        "body_fr": (
            "Nous recueillons uniquement les informations nécessaires pour répondre "
            "aux messages de contact et aux demandes de réservation locales. Les "
            "données de carte de paiement ne sont pas collectées à ce stade et les "
            "données des voyageurs ne sont pas affichées publiquement. Cette version "
            "doit faire l’objet d’une révision juridique avant le lancement."
        ),
    },
    "cancellation": {
        "title_fr": "Politique d’annulation",
        "body_fr": (
            "La politique définitive d’annulation et de remboursement n’a pas encore "
            "été approuvée. Une demande d’annulation locale reste en cours d’examen, "
            "ne modifie pas automatiquement la réservation d’origine et ne déclenche "
            "aucun remboursement."
        ),
    },
    "cookies": {
        "title_fr": "Politique relative aux cookies",
        "body_fr": (
            "Le site utilise des cookies essentiels pour les sessions, la sécurité "
            "et le choix de la langue. Les outils d’analyse et de publicité ne sont "
            "pas activés à ce stade. Cette politique sera mise à jour avant "
            "l’activation de tout outil non essentiel."
        ),
    },
}

FAQS = {
    "Is the displayed price final?": {
        "question_fr": "Le prix affiché est-il définitif ?",
        "answer_fr": (
            "Le prix est temporaire et provient du système d’exploitation. Il est "
            "vérifié à nouveau avant la poursuite d’une demande de réservation. Le "
            "paiement n’est pas encore activé."
        ),
    },
    "Does submitting guest details confirm a booking?": {
        "question_fr": (
            "L’envoi des informations du voyageur confirme-t-il la réservation ?"
        ),
        "answer_fr": (
            "Non. Une demande locale n’est confirmée qu’après le paiement et la "
            "création de la réservation. Ces deux étapes sont désactivées à ce stade."
        ),
    },
    "When is the full address shared?": {
        "question_fr": "Quand l’adresse complète est-elle communiquée ?",
        "answer_fr": (
            "L’adresse privée complète n’est pas affichée sur les pages publiques. "
            "Les informations d’arrivée seront communiquées dans le parcours de "
            "réservation confirmée lorsqu’il sera activé."
        ),
    },
}


def seed_french_content(apps, schema_editor):
    del schema_editor
    site_page = apps.get_model("core", "SitePage")
    faq_item = apps.get_model("core", "FAQItem")
    for slug, values in PAGES.items():
        site_page.objects.filter(slug=slug, title_fr="").update(**values)
    for question_en, values in FAQS.items():
        faq_item.objects.filter(question_en=question_en, question_fr="").update(**values)


def clear_seeded_french_content(apps, schema_editor):
    del schema_editor
    site_page = apps.get_model("core", "SitePage")
    faq_item = apps.get_model("core", "FAQItem")
    for slug, values in PAGES.items():
        site_page.objects.filter(slug=slug, **values).update(title_fr="", body_fr="")
    for question_en, values in FAQS.items():
        faq_item.objects.filter(question_en=question_en, **values).update(
            question_fr="",
            answer_fr="",
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0006_faqitem_answer_fr_faqitem_question_fr_and_more")]

    operations = [
        migrations.RunPython(seed_french_content, clear_seeded_french_content),
    ]
