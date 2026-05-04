// ============================================================
// TEMPLATE DELIVERY CONFIG
// Fill in your Notion share URLs below, then save.
// Everything updates automatically — no other files to edit.
// ============================================================
window.TEMPLATE_CONFIG = {
  freelanceos_pro: {
    name: "FreelanceOS Pro",
    notion_url: "PASTE_YOUR_FREELANCEOS_PRO_NOTION_SHARE_URL_HERE",
    gumroad_url: "https://markmma1985.gumroad.com/l/fbbmmc",
    emoji: "⚡",
    color: "#7c3aed",
    onboarding_steps: [
      "Click the template link below",
      "In Notion, click 'Duplicate' in the top-right",
      "Choose your workspace",
      "Start with the Getting Started page inside the template"
    ],
    quick_wins: [
      "Add your first client in the Client CRM",
      "Create your first project and link it to the client",
      "Set up your invoice template with your business details"
    ]
  },
  creatorhq_pro: {
    name: "CreatorHQ Pro",
    notion_url: "PASTE_YOUR_CREATORHQ_PRO_NOTION_SHARE_URL_HERE",
    gumroad_url: "https://markmma1985.gumroad.com/l/fldlij",
    emoji: "🎨",
    color: "#ec4899",
    onboarding_steps: [
      "Click the template link below",
      "In Notion, click 'Duplicate' in the top-right",
      "Choose your workspace",
      "Open the Content Calendar and add your first 3 content ideas"
    ],
    quick_wins: [
      "Add your social media channels to the Channels database",
      "Create your first content piece in the pipeline",
      "Add any active brand deals in the Brand Deals tracker"
    ]
  },
  agencyos: {
    name: "AgencyOS",
    notion_url: "PASTE_YOUR_AGENCYOS_NOTION_SHARE_URL_HERE",
    gumroad_url: null,
    emoji: "🏢",
    color: "#10b981",
    onboarding_steps: [
      "Click the template link below",
      "In Notion, click 'Duplicate' in the top-right",
      "Choose your workspace",
      "Start by adding your first client to the Client Dashboard"
    ],
    quick_wins: [
      "Add all current clients with their MRR values",
      "Set up your team members in the Team database",
      "Import your active projects into the Project Tracker"
    ]
  },
  youtube_empire: {
    name: "YouTube Empire",
    notion_url: "PASTE_YOUR_YOUTUBE_EMPIRE_NOTION_SHARE_URL_HERE",
    gumroad_url: null,
    emoji: "🎬",
    color: "#ef4444",
    onboarding_steps: [
      "Click the template link below",
      "In Notion, click 'Duplicate' in the top-right",
      "Choose your workspace",
      "Add your YouTube channel details to the Channel Overview"
    ],
    quick_wins: [
      "Add your top 5 video ideas to the Ideas Vault",
      "Set up your channel analytics baseline",
      "Add any active sponsorship conversations to the Brand Deals tracker"
    ]
  },
  bundle: {
    name: "FreelanceOS + CreatorHQ Bundle",
    notion_url: null, // bundle delivers both templates separately
    gumroad_url: null,
    emoji: "🚀",
    color: "#3b82f6",
    bundle_products: ["freelanceos_pro", "creatorhq_pro"],
    onboarding_steps: [
      "You get BOTH templates — click each link below",
      "Duplicate each one to your Notion workspace",
      "Start with FreelanceOS for client/project management",
      "Use CreatorHQ for your content pipeline"
    ],
    quick_wins: [
      "Set up FreelanceOS first — add your first client",
      "Then open CreatorHQ and plan your first content week",
      "Link your content projects back to clients in FreelanceOS"
    ]
  }
};
