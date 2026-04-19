// Inline SVG icon set — avoids Material Symbols ligature issues with Tailwind CDN preflight.
function Icon({ name, size = 14, color = "currentColor", style = {} }) {
  const s = { width: size, height: size, flexShrink: 0, verticalAlign: "middle", ...style };
  const stroke = color;
  const common = { width: size, height: size, viewBox: "0 0 24 24", fill: "none",
                   stroke, strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round", style: s };
  switch (name) {
    case "gavel":       return <svg {...common}><path d="M14 3l7 7M11 6l7 7M9 8l-5 5 3 3 5-5M3 21h10"/></svg>;
    case "hub":         return <svg {...common}><circle cx="12" cy="12" r="2.5"/><circle cx="4" cy="5" r="1.8"/><circle cx="20" cy="5" r="1.8"/><circle cx="4" cy="19" r="1.8"/><circle cx="20" cy="19" r="1.8"/><path d="M5.5 6l4.5 4.5M18.5 6L14 10.5M5.5 18L10 13.5M18.5 18L14 13.5"/></svg>;
    case "scale":       return <svg {...common}><path d="M12 3v18M6 7h12M6 7l-3 7a3 3 0 006 0l-3-7M18 7l-3 7a3 3 0 006 0l-3-7M5 21h14"/></svg>;
    case "timeline":    return <svg {...common}><circle cx="6" cy="12" r="2"/><circle cx="18" cy="12" r="2"/><circle cx="12" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><path d="M8 12h2M14 12h2M12 8v2M12 14v2"/></svg>;
    case "euro":        return <svg {...common}><path d="M18 7a6 6 0 100 10M3 10h10M3 14h10"/></svg>;
    case "add":         return <svg {...common}><path d="M12 5v14M5 12h14"/></svg>;
    case "sparkle":     return <svg {...common}><path d="M12 3l2 6 6 2-6 2-2 6-2-6-6-2 6-2 2-6zM19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14z"/></svg>;
    case "contrast":    return <svg {...common}><circle cx="12" cy="12" r="9"/><path d="M12 3v18" fill={stroke} stroke="none"/><path d="M12 3a9 9 0 010 18V3z" fill={stroke} stroke="none"/></svg>;
    case "refresh":     return <svg {...common}><path d="M3 12a9 9 0 1015-6.7M20 4v5h-5"/></svg>;
    case "info":        return <svg {...common}><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v5h1"/></svg>;
    case "bolt":        return <svg {...common}><path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z"/></svg>;
    case "close":       return <svg {...common}><path d="M6 6l12 12M6 18L18 6"/></svg>;
    case "chevron-left":return <svg {...common}><path d="M15 18l-6-6 6-6"/></svg>;
    case "chevron-right":return <svg {...common}><path d="M9 18l6-6-6-6"/></svg>;
    case "chevron-down":return <svg {...common}><path d="M6 9l6 6 6-6"/></svg>;
    case "arrow-right": return <svg {...common}><path d="M5 12h14M13 6l6 6-6 6"/></svg>;
    case "check":       return <svg {...common}><path d="M5 13l4 4 10-10"/></svg>;
    case "filter":      return <svg {...common}><path d="M4 5h16l-6 8v5l-4 2v-7z"/></svg>;
    case "search":      return <svg {...common}><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>;
    case "tune":        return <svg {...common}><path d="M4 7h10M18 7h2M4 17h2M10 17h10M14 5v4M8 15v4"/></svg>;
    case "hourglass":   return <svg {...common}><path d="M6 3h12M6 21h12M7 3v4a5 5 0 0010 0V3M7 21v-4a5 5 0 0110 0v4"/></svg>;
    case "dots":        return <svg {...common}><circle cx="6" cy="12" r="1.5" fill={stroke}/><circle cx="12" cy="12" r="1.5" fill={stroke}/><circle cx="18" cy="12" r="1.5" fill={stroke}/></svg>;
    case "attach":      return <svg {...common}><path d="M21 11l-8.5 8.5a5 5 0 01-7-7L14 4a3.5 3.5 0 015 5l-8.5 8.5a2 2 0 01-3-3L15 7"/></svg>;
    case "home":        return <svg {...common}><path d="M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10"/></svg>;
    case "triage":      return <svg {...common}><rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><path d="M7 7h.01M7 17h.01"/></svg>;
    case "folder":      return <svg {...common}><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/></svg>;
    case "command":     return <svg {...common}><path d="M9 3a3 3 0 010 6H3V6a3 3 0 016-3zM15 21a3 3 0 010-6h6v3a3 3 0 01-6 3zM9 21a3 3 0 000-6H3v3a3 3 0 006 3zM15 3a3 3 0 000 6h6V6a3 3 0 00-6-3zM9 9h6v6H9z"/></svg>;
    case "upload":      return <svg {...common}><path d="M12 4v12M6 10l6-6 6 6M4 20h16"/></svg>;
    case "bell":        return <svg {...common}><path d="M6 16V11a6 6 0 0112 0v5l2 2H4l2-2zM10 20a2 2 0 004 0"/></svg>;
    case "settings":    return <svg {...common}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>;
    case "user":        return <svg {...common}><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0116 0"/></svg>;
    case "dot":         return <svg {...common}><circle cx="12" cy="12" r="3" fill={stroke}/></svg>;
    default: return null;
  }
}
window.Icon = Icon;
