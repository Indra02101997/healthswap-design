"""The HealthSwap recommendation label and localized shopper-facing copy (Recommendation/Labels.cs)."""

from __future__ import annotations

from dataclasses import dataclass

from healthswap.domain.recommendation.explanations import ExplanationTemplates


@dataclass(frozen=True, slots=True)
class RecommendationLabel:
    """The label every HealthSwap-recommended product carries (RA-1, docs/06-recommended-label-and-add-fee.md)."""

    code: str
    text: str
    short_text: str
    disclosure: str
    chargeable: bool
    fee_rule: str | None


_BADGE_LABELS: dict[tuple[str, str], str] = {
    ("hi", "HIGH_SUGAR"): "अधिक चीनी",
    ("hi", "HIGH_SODIUM"): "अधिक सोडियम",
    ("hi", "HIGH_SAT_FAT"): "अधिक संतृप्त वसा",
    ("hi", "WHOLE_GRAIN"): "साबुत अनाज",
    ("hi", "HIGH_FIBRE"): "अधिक फाइबर",
    ("hi", "HIGH_PROTEIN"): "अधिक प्रोटीन",
    ("hi", "NO_ADDED_SUGAR"): "कोई अतिरिक्त चीनी नहीं",
    ("hi", "ULTRA_PROCESSED"): "अति-प्रसंस्कृत",
    ("es", "HIGH_SUGAR"): "Alto en azúcar",
    ("es", "HIGH_SODIUM"): "Alto en sodio",
    ("es", "HIGH_SAT_FAT"): "Alto en grasa saturada",
    ("es", "WHOLE_GRAIN"): "Grano entero",
    ("es", "HIGH_FIBRE"): "Alto en fibra",
    ("es", "HIGH_PROTEIN"): "Alto en proteína",
    ("es", "NO_ADDED_SUGAR"): "Sin azúcar añadido",
    ("es", "ULTRA_PROCESSED"): "Ultraprocesado",
}


class ShopperCopy:
    """Localized shopper-facing copy: recommendation label, fee disclosure and label disclaimer (doc 04 4.3)."""

    RECOMMENDED_CODE = "HEALTHSWAP_RECOMMENDED"

    @staticmethod
    def label(locale: str, partner_name: str, add_fee_enabled: bool) -> RecommendationLabel:
        return RecommendationLabel(
            ShopperCopy.RECOMMENDED_CODE, ShopperCopy.label_text(locale), ShopperCopy.short_label(locale),
            ShopperCopy.fee_disclosure(locale, partner_name, add_fee_enabled), add_fee_enabled, "RA-1" if add_fee_enabled else None,
        )

    @staticmethod
    def label_text(locale: str) -> str:
        lang = ExplanationTemplates.lang(locale)
        if lang == "hi":
            return "HealthSwap द्वारा सुझाया गया"
        if lang == "es":
            return "Recomendado por HealthSwap"
        return "Recommended by HealthSwap"

    @staticmethod
    def short_label(locale: str) -> str:
        lang = ExplanationTemplates.lang(locale)
        if lang == "hi":
            return "HealthSwap की पसंद"
        if lang == "es":
            return "Selección de HealthSwap"
        return "HealthSwap pick"

    @staticmethod
    def fee_disclosure(locale: str, partner_name: str, add_fee_enabled: bool) -> str:
        """Material-connection disclosure (FTC Endorsement Guides, India CCPA 2023). Never states the amount."""
        lang = ExplanationTemplates.lang(locale)
        if add_fee_enabled:
            if lang == "hi":
                return (
                    f"जब आप सुझाया गया आइटम कार्ट में जोड़ते हैं या सुझाया गया विकल्प खरीदते हैं, तो HealthSwap को {partner_name} से शुल्क मिल सकता है। "
                    "सुझाव पोषण के आधार पर तय होते हैं, शुल्क के आधार पर नहीं।"
                )
            if lang == "es":
                return (
                    f"HealthSwap puede recibir una tarifa de {partner_name} cuando agregas un artículo recomendado a tu carrito o compras un cambio sugerido. "
                    "Las recomendaciones se clasifican por nutrición, no por tarifas."
                )
            return (
                f"HealthSwap may receive a fee from {partner_name} when you add a recommended item to your cart or buy a suggested swap. "
                "Recommendations are ranked on nutrition, not fees."
            )
        if lang == "hi":
            return f"जब आप सुझाया गया आइटम चुनते हैं, तो HealthSwap को {partner_name} से शुल्क मिल सकता है। सुझाव पोषण के आधार पर तय होते हैं, शुल्क के आधार पर नहीं।"
        if lang == "es":
            return (
                f"HealthSwap puede recibir una tarifa de {partner_name} cuando eliges un artículo sugerido. "
                "Las sugerencias se clasifican por nutrición, no por tarifas."
            )
        return f"HealthSwap may receive a fee from {partner_name} when you choose a suggested item. Suggestions are ranked on nutrition, not fees."

    @staticmethod
    def label_disclaimer(locale: str, liquid: bool) -> str:
        lang = ExplanationTemplates.lang(locale)
        if lang == "hi":
            return (
                "पैक लेबल डेटा (प्रति 100 मिली) पर आधारित। एलर्जी होने पर हमेशा लेबल देखें।"
                if liquid
                else "पैक लेबल डेटा (प्रति 100 ग्राम) पर आधारित। एलर्जी होने पर हमेशा लेबल देखें।"
            )
        if lang == "es":
            return (
                "Basado en los datos de la etiqueta por 100 ml. Revisa siempre la etiqueta, especialmente si tienes alergias."
                if liquid
                else "Basado en los datos de la etiqueta por 100 g. Revisa siempre la etiqueta, especialmente si tienes alergias."
            )
        return (
            "Based on pack label data per 100 ml. Always check the label, especially for allergies."
            if liquid
            else "Based on pack label data per 100 g. Always check the label, especially for allergies."
        )

    @staticmethod
    def generic_disclaimer(locale: str) -> str:
        lang = ExplanationTemplates.lang(locale)
        if lang == "hi":
            return "पैक लेबल डेटा पर आधारित। एलर्जी होने पर हमेशा लेबल देखें।"
        if lang == "es":
            return "Basado en los datos de la etiqueta. Revisa siempre la etiqueta, especialmente si tienes alergias."
        return "Based on pack label data. Always check the label, especially for allergies."

    @staticmethod
    def badge_label(code: str, fallback: str, locale: str) -> str:
        return _BADGE_LABELS.get((ExplanationTemplates.lang(locale), code), fallback)
