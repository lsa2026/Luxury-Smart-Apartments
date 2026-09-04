"""Hand-written Arabic and French names for the amenities Hostaway reports.

Every entry here was written by a person, not produced by a translation service:
the site does not machine-translate source content, and an amenity label is read
by a guest deciding whether a home suits them.

The key is the English name exactly as Hostaway returns it, matched case
insensitively. An amenity missing from this map keeps its English name rather
than being guessed at, and the seeding command reports it so the gap is visible.

``category`` groups the list on the property page; ``icon_key`` names the glyph
the template may use. Both are optional and safe to leave empty.
"""

from typing import Final, NamedTuple

from django.utils.translation import gettext_lazy as _


class AmenityCopy(NamedTuple):
    name_ar: str
    name_fr: str
    category: str
    icon_key: str


# Category codes are stable identifiers; the label shown to a guest is
# translated at display time from CATEGORY_LABELS below.
ESSENTIALS: Final = "essentials"
KITCHEN: Final = "kitchen"
COMFORT: Final = "comfort"
LAUNDRY: Final = "laundry"
FAMILY: Final = "family"
SAFETY: Final = "safety"
OUTDOOR: Final = "outdoor"

CATEGORY_LABELS: Final = {
    ESSENTIALS: _("Essentials"),
    KITCHEN: _("Kitchen and dining"),
    COMFORT: _("Comfort and entertainment"),
    LAUNDRY: _("Laundry"),
    FAMILY: _("Family"),
    SAFETY: _("Safety"),
    OUTDOOR: _("Outdoor and parking"),
}

AMENITY_COPY: Final[dict[str, AmenityCopy]] = {
    # --- essentials ---------------------------------------------------------
    "internet": AmenityCopy("إنترنت", "Internet", ESSENTIALS, "wifi"),
    "wireless": AmenityCopy("واي فاي", "Wi-Fi", ESSENTIALS, "wifi"),
    "essentials": AmenityCopy("مستلزمات أساسية", "Nécessaire de base", ESSENTIALS, "essentials"),
    "hot water": AmenityCopy("ماء ساخن", "Eau chaude", ESSENTIALS, "hot-water"),
    "linens": AmenityCopy("مفروشات وأغطية", "Linge de maison", ESSENTIALS, "linens"),
    "hangers": AmenityCopy("علّاقات ملابس", "Cintres", ESSENTIALS, "hangers"),
    "clothing storage": AmenityCopy(
        "خزانة ملابس", "Rangement pour vêtements", ESSENTIALS, "wardrobe"
    ),
    "cleaning products": AmenityCopy("مواد تنظيف", "Produits d'entretien", ESSENTIALS, "cleaning"),
    # --- kitchen and dining -------------------------------------------------
    "kitchen": AmenityCopy("مطبخ", "Cuisine", KITCHEN, "kitchen"),
    "kitchen utensils": AmenityCopy("أدوات مطبخ", "Ustensiles de cuisine", KITCHEN, "utensils"),
    "cooking basics": AmenityCopy("أساسيات الطهي", "Nécessaire de cuisine", KITCHEN, "cooking"),
    "refrigerator": AmenityCopy("ثلاجة", "Réfrigérateur", KITCHEN, "fridge"),
    "oven": AmenityCopy("فرن", "Four", KITCHEN, "oven"),
    "coffee/tea maker": AmenityCopy(
        "صانعة قهوة وشاي", "Cafetière et bouilloire", KITCHEN, "coffee"
    ),
    "dining table": AmenityCopy("طاولة طعام", "Table à manger", KITCHEN, "dining"),
    # --- comfort and entertainment ------------------------------------------
    "air conditioning": AmenityCopy("تكييف", "Climatisation", COMFORT, "air-conditioning"),
    "tv": AmenityCopy("تلفزيون", "Télévision", COMFORT, "tv"),
    "room darkening shades": AmenityCopy("ستائر معتمة", "Rideaux occultants", COMFORT, "blinds"),
    "exercise equipment": AmenityCopy("أجهزة رياضية", "Équipement de sport", COMFORT, "gym"),
    "bidet": AmenityCopy("شطّاف", "Bidet", COMFORT, "bidet"),
    "hair dryer": AmenityCopy("مجفف شعر", "Sèche-cheveux", COMFORT, "hair-dryer"),
    # --- laundry ------------------------------------------------------------
    "washing machine": AmenityCopy("غسالة ملابس", "Lave-linge", LAUNDRY, "washer"),
    "dryer": AmenityCopy("مجفف ملابس", "Sèche-linge", LAUNDRY, "dryer"),
    "iron": AmenityCopy("مكواة", "Fer à repasser", LAUNDRY, "iron"),
    # --- family -------------------------------------------------------------
    "baby crib": AmenityCopy("سرير أطفال", "Lit bébé", FAMILY, "crib"),
    "suitable for children": AmenityCopy("مناسب للأطفال", "Adapté aux enfants", FAMILY, "children"),
    "suitable for infants": AmenityCopy("مناسب للرُّضّع", "Adapté aux nourrissons", FAMILY, "infant"),
    # --- safety -------------------------------------------------------------
    "smoke detector": AmenityCopy("كاشف دخان", "Détecteur de fumée", SAFETY, "smoke-detector"),
    "carbon monoxide detector": AmenityCopy(
        "كاشف أول أكسيد الكربون",
        "Détecteur de monoxyde de carbone",
        SAFETY,
        "co-detector",
    ),
    "fire extinguisher": AmenityCopy("طفاية حريق", "Extincteur", SAFETY, "extinguisher"),
    "first aid kit": AmenityCopy(
        "حقيبة إسعافات أولية", "Trousse de premiers secours", SAFETY, "first-aid"
    ),
    # --- outdoor and parking ------------------------------------------------
    "free parking": AmenityCopy("موقف مجاني", "Parking gratuit", OUTDOOR, "parking"),
    "swimming pool": AmenityCopy("مسبح", "Piscine", OUTDOOR, "pool"),
}


def copy_for(source_name: str) -> AmenityCopy | None:
    """Look up an amenity by the English name Hostaway returned."""
    return AMENITY_COPY.get(" ".join((source_name or "").split()).casefold())
