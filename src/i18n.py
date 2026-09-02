LANGUAGES = {
    "en": "English",
    "hi": "हिन्दी",
    "bn": "বাংলা",
}

MESSAGES = {
    "en": {
        "choose_language": "Choose your language:",
        "language_saved": "✅ Language saved: {language}",
        "welcome": "Welcome to <b>NexusFilterBot</b>. Add me to a group, then send a file name or title to search the shared library.",
        "no_results": "No results for <b>{query}</b>.",
        "delivery_ready": "Your private delivery link is ready for 10 minutes.",
        "send_to_dm": "📥 Send to my DM (10 min)",
        "join_required": "Join the required updates channel first, then try again.",
    },
    "hi": {
        "choose_language": "अपनी भाषा चुनें:",
        "language_saved": "✅ भाषा सहेजी गई: {language}",
        "welcome": "<b>NexusFilterBot</b> में आपका स्वागत है। मुझे ग्रुप में जोड़ें और साझा लाइब्रेरी खोजने के लिए फ़ाइल का नाम या शीर्षक भेजें।",
        "no_results": "<b>{query}</b> के लिए कोई परिणाम नहीं मिला।",
        "delivery_ready": "आपका निजी डिलीवरी लिंक 10 मिनट के लिए तैयार है।",
        "send_to_dm": "📥 मेरे DM में भेजें (10 मिनट)",
        "join_required": "पहले आवश्यक अपडेट चैनल जॉइन करें, फिर दोबारा प्रयास करें।",
    },
    "bn": {
        "choose_language": "আপনার ভাষা বেছে নিন:",
        "language_saved": "✅ ভাষা সংরক্ষণ করা হয়েছে: {language}",
        "welcome": "<b>NexusFilterBot</b>-এ স্বাগতম। আমাকে একটি গ্রুপে যোগ করুন, তারপর শেয়ার করা লাইব্রেরি খুঁজতে ফাইলের নাম বা শিরোনাম পাঠান।",
        "no_results": "<b>{query}</b>-এর কোনো ফল পাওয়া যায়নি।",
        "delivery_ready": "আপনার ব্যক্তিগত ডেলিভারি লিঙ্ক ১০ মিনিটের জন্য প্রস্তুত।",
        "send_to_dm": "📥 আমার DM-এ পাঠান (১০ মিনিট)",
        "join_required": "আগে প্রয়োজনীয় আপডেট চ্যানেলে যোগ দিন, তারপর আবার চেষ্টা করুন।",
    },
}


def translate(language: str | None, key: str, **values: str) -> str:
    template = MESSAGES.get(language or "en", MESSAGES["en"]).get(key, MESSAGES["en"][key])
    return template.format(**values)
