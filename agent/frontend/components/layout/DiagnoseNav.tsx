"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils/cn";

const tabs = [
  { href: "/diagnose", label: "Incident" },
  { href: "/diagnose/range", label: "Time range" },
] as const;

export function DiagnoseNav() {
  const pathname = usePathname();
  return (
    <nav className="mb-6 flex gap-1 border-b border-border">
      {tabs.map((tab) => {
        const active =
          tab.href === "/diagnose"
            ? pathname === "/diagnose"
            : pathname.startsWith(tab.href);
        return (
          <Link
            key={tab.href}
            href={tab.href}
            className={cn(
              "border-b-2 px-4 py-2 text-sm font-medium transition-colors",
              active
                ? "border-accent text-foreground"
                : "border-transparent text-muted hover:text-foreground",
            )}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
