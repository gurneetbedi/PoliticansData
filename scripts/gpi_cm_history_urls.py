"""Wikipedia article URLs for each state's Chief Minister history.

The "List of chief ministers of X" pages have a canonical tenure table:
    Name | Portrait | Constituency | Term of office (from → to) | Party | Election

We scrape this table for every state to build a complete CM tenure history.
"""
WIKIPEDIA_CM_LIST_URLS = {
    "Andhra Pradesh":     "https://en.wikipedia.org/wiki/Chief_Minister_of_Andhra_Pradesh",
    "Arunachal Pradesh":  "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Arunachal_Pradesh",
    "Assam":              "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Assam",
    "Bihar":              "https://en.wikipedia.org/wiki/Chief_Minister_of_Bihar",
    "Chhattisgarh":       "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Chhattisgarh",
    "Goa":                "https://en.wikipedia.org/wiki/Chief_Minister_of_Goa",
    "Gujarat":            "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Gujarat",
    "Haryana":            "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Haryana",
    "Himachal Pradesh":   "https://en.wikipedia.org/wiki/Chief_Minister_of_Himachal_Pradesh",
    "Jharkhand":          "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Jharkhand",
    "Karnataka":          "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Karnataka",
    "Kerala":             "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Kerala",
    "Madhya Pradesh":     "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Madhya_Pradesh",
    "Maharashtra":        "https://en.wikipedia.org/wiki/Chief_Minister_of_Maharashtra",
    "Manipur":            "https://en.wikipedia.org/wiki/Chief_Minister_of_Manipur",
    "Meghalaya":          "https://en.wikipedia.org/wiki/Chief_Minister_of_Meghalaya",
    "Mizoram":            "https://en.wikipedia.org/wiki/Chief_Minister_of_Mizoram",
    "Nagaland":           "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Nagaland",
    "Odisha":             "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Odisha",
    "Punjab":             "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Punjab,_India",
    "Rajasthan":          "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Rajasthan",
    "Sikkim":             "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Sikkim",
    "Tamil Nadu":         "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Tamil_Nadu",
    "Telangana":          "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Telangana",
    "Tripura":            "https://en.wikipedia.org/wiki/Chief_Minister_of_Tripura",
    "Uttar Pradesh":      "https://en.wikipedia.org/wiki/Chief_Minister_of_Uttar_Pradesh",
    "Uttarakhand":        "https://en.wikipedia.org/wiki/Chief_Minister_of_Uttarakhand",
    "West Bengal":        "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_West_Bengal",

    "Delhi":              "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Delhi",
    "Jammu and Kashmir":  "https://en.wikipedia.org/wiki/List_of_chief_ministers_of_Jammu_and_Kashmir",
    "Puducherry":         "https://en.wikipedia.org/wiki/Chief_Minister_of_Puducherry",
}
