"""Wikipedia article URLs for each state's current cabinet.

Naming conventions vary — some states have a dedicated "Council of Ministers"
page, others just have the ministry-formation article (e.g., "Second Bhagwant
Mann ministry"). The linked page must contain a wikitable with columns
matching {Minister, Portfolio, Party}.

To add a state: find the most current cabinet article on Wikipedia, verify it
has a portfolios table, add the URL below with the exact state name we use
in our `states` table.
"""
WIKIPEDIA_CABINET_URLS = {
    # ─── States (28) ──────────────────────────────────────────────────
    "Andhra Pradesh":     "https://en.wikipedia.org/wiki/Fourth_N._Chandrababu_Naidu_ministry",
    "Arunachal Pradesh":  "https://en.wikipedia.org/wiki/Fifth_Pema_Khandu_ministry",
    "Assam":              "https://en.wikipedia.org/wiki/Second_Sarma_ministry",
    "Bihar":              "https://en.wikipedia.org/wiki/Tenth_Nitish_Kumar_ministry",
    "Chhattisgarh":       "https://en.wikipedia.org/wiki/Sai_ministry",
    "Goa":                "https://en.wikipedia.org/wiki/Second_Pramod_Sawant_ministry",
    "Gujarat":            "https://en.wikipedia.org/wiki/Second_Bhupendrabhai_Patel_ministry",
    "Haryana":            "https://en.wikipedia.org/wiki/Second_Saini_ministry",
    "Himachal Pradesh":   "https://en.wikipedia.org/wiki/Sukhu_ministry",
    "Jharkhand":          "https://en.wikipedia.org/wiki/Third_Hemant_Soren_ministry",
    "Karnataka":          "https://en.wikipedia.org/wiki/Second_Siddaramaiah_ministry",
    "Kerala":             "https://en.wikipedia.org/wiki/Second_Pinarayi_Vijayan_ministry",
    "Madhya Pradesh":     "https://en.wikipedia.org/wiki/Mohan_Yadav_ministry",
    "Maharashtra":        "https://en.wikipedia.org/wiki/Third_Fadnavis_ministry",
    "Manipur":            "https://en.wikipedia.org/wiki/Second_N._Biren_Singh_ministry",
    "Meghalaya":          "https://en.wikipedia.org/wiki/Second_Conrad_Sangma_ministry",
    "Mizoram":            "https://en.wikipedia.org/wiki/Lalduhoma_ministry",
    "Nagaland":           "https://en.wikipedia.org/wiki/Fifth_Rio_ministry",
    "Odisha":             "https://en.wikipedia.org/wiki/Majhi_ministry",
    "Punjab":             "https://en.wikipedia.org/wiki/Bhagwant_Mann_ministry",
    "Rajasthan":          "https://en.wikipedia.org/wiki/Bhajan_Lal_Sharma_ministry",
    "Sikkim":             "https://en.wikipedia.org/wiki/Second_Tamang_ministry",
    "Tamil Nadu":         "https://en.wikipedia.org/wiki/M._K._Stalin_ministry",
    "Telangana":          "https://en.wikipedia.org/wiki/Revanth_Reddy_ministry",
    "Tripura":            "https://en.wikipedia.org/wiki/Second_Saha_ministry",
    "Uttar Pradesh":      "https://en.wikipedia.org/wiki/Second_Yogi_Adityanath_ministry",
    "Uttarakhand":        "https://en.wikipedia.org/wiki/Second_Dhami_ministry",
    "West Bengal":        "https://en.wikipedia.org/wiki/Third_Mamata_Banerjee_ministry",

    # ─── UTs with legislature (3) ─────────────────────────────────────
    "Delhi":              "https://en.wikipedia.org/wiki/Rekha_Gupta_ministry",
    "Jammu and Kashmir":  "https://en.wikipedia.org/wiki/Second_Omar_Abdullah_ministry",
    "Puducherry":         "https://en.wikipedia.org/wiki/Fifth_Rangaswamy_ministry",
}
