namespace HealthSwap.Domain.Recommendation;

/// <summary>The label every HealthSwap-recommended product carries (RA-1, docs/06-recommended-label-and-add-fee.md).</summary>
public sealed record RecommendationLabel(string Code, string Text, string ShortText, string Disclosure, bool Chargeable, string? FeeRule);

/// <summary>Localized shopper-facing copy: recommendation label, fee disclosure and label disclaimer (doc 04 4.3).</summary>
public static class ShopperCopy
{
    public const string RecommendedCode = "HEALTHSWAP_RECOMMENDED";

    public static RecommendationLabel Label(string locale, string partnerName, bool addFeeEnabled) =>
        new(RecommendedCode, LabelText(locale), ShortLabel(locale), FeeDisclosure(locale, partnerName, addFeeEnabled), addFeeEnabled,
            addFeeEnabled ? "RA-1" : null);

    public static string LabelText(string locale) => ExplanationTemplates.Lang(locale) switch
    {
        "hi" => "HealthSwap द्वारा सुझाया गया",
        "es" => "Recomendado por HealthSwap",
        _ => "Recommended by HealthSwap",
    };

    public static string ShortLabel(string locale) => ExplanationTemplates.Lang(locale) switch
    {
        "hi" => "HealthSwap की पसंद",
        "es" => "Selección de HealthSwap",
        _ => "HealthSwap pick",
    };

    /// <summary>Material-connection disclosure (FTC Endorsement Guides, India CCPA 2023). Never states the amount.</summary>
    public static string FeeDisclosure(string locale, string partnerName, bool addFeeEnabled)
    {
        var lang = ExplanationTemplates.Lang(locale);
        if (addFeeEnabled)
            return lang switch
            {
                "hi" => $"जब आप सुझाया गया आइटम कार्ट में जोड़ते हैं या सुझाया गया विकल्प खरीदते हैं, तो HealthSwap को {partnerName} से शुल्क मिल सकता है। सुझाव पोषण के आधार पर तय होते हैं, शुल्क के आधार पर नहीं।",
                "es" => $"HealthSwap puede recibir una tarifa de {partnerName} cuando agregas un artículo recomendado a tu carrito o compras un cambio sugerido. Las recomendaciones se clasifican por nutrición, no por tarifas.",
                _ => $"HealthSwap may receive a fee from {partnerName} when you add a recommended item to your cart or buy a suggested swap. Recommendations are ranked on nutrition, not fees.",
            };
        return lang switch
        {
            "hi" => $"जब आप सुझाया गया आइटम चुनते हैं, तो HealthSwap को {partnerName} से शुल्क मिल सकता है। सुझाव पोषण के आधार पर तय होते हैं, शुल्क के आधार पर नहीं।",
            "es" => $"HealthSwap puede recibir una tarifa de {partnerName} cuando eliges un artículo sugerido. Las sugerencias se clasifican por nutrición, no por tarifas.",
            _ => $"HealthSwap may receive a fee from {partnerName} when you choose a suggested item. Suggestions are ranked on nutrition, not fees.",
        };
    }

    public static string LabelDisclaimer(string locale, bool liquid) => ExplanationTemplates.Lang(locale) switch
    {
        "hi" => liquid ? "पैक लेबल डेटा (प्रति 100 मिली) पर आधारित। एलर्जी होने पर हमेशा लेबल देखें।" : "पैक लेबल डेटा (प्रति 100 ग्राम) पर आधारित। एलर्जी होने पर हमेशा लेबल देखें।",
        "es" => liquid ? "Basado en los datos de la etiqueta por 100 ml. Revisa siempre la etiqueta, especialmente si tienes alergias." : "Basado en los datos de la etiqueta por 100 g. Revisa siempre la etiqueta, especialmente si tienes alergias.",
        _ => liquid ? "Based on pack label data per 100 ml. Always check the label, especially for allergies." : "Based on pack label data per 100 g. Always check the label, especially for allergies.",
    };

    public static string GenericDisclaimer(string locale) => ExplanationTemplates.Lang(locale) switch
    {
        "hi" => "पैक लेबल डेटा पर आधारित। एलर्जी होने पर हमेशा लेबल देखें।",
        "es" => "Basado en los datos de la etiqueta. Revisa siempre la etiqueta, especialmente si tienes alergias.",
        _ => "Based on pack label data. Always check the label, especially for allergies.",
    };

    public static string BadgeLabel(string code, string fallback, string locale) => (ExplanationTemplates.Lang(locale), code) switch
    {
        ("hi", "HIGH_SUGAR") => "अधिक चीनी",
        ("hi", "HIGH_SODIUM") => "अधिक सोडियम",
        ("hi", "HIGH_SAT_FAT") => "अधिक संतृप्त वसा",
        ("hi", "WHOLE_GRAIN") => "साबुत अनाज",
        ("hi", "HIGH_FIBRE") => "अधिक फाइबर",
        ("hi", "HIGH_PROTEIN") => "अधिक प्रोटीन",
        ("hi", "NO_ADDED_SUGAR") => "कोई अतिरिक्त चीनी नहीं",
        ("hi", "ULTRA_PROCESSED") => "अति-प्रसंस्कृत",
        ("es", "HIGH_SUGAR") => "Alto en azúcar",
        ("es", "HIGH_SODIUM") => "Alto en sodio",
        ("es", "HIGH_SAT_FAT") => "Alto en grasa saturada",
        ("es", "WHOLE_GRAIN") => "Grano entero",
        ("es", "HIGH_FIBRE") => "Alto en fibra",
        ("es", "HIGH_PROTEIN") => "Alto en proteína",
        ("es", "NO_ADDED_SUGAR") => "Sin azúcar añadido",
        ("es", "ULTRA_PROCESSED") => "Ultraprocesado",
        _ => fallback,
    };
}
