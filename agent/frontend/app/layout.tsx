import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { Header } from "@/components/layout/Header";
import { SourceHealthBanner } from "@/components/layout/SourceHealthBanner";
import { StatusBar } from "@/components/layout/StatusBar";

import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "LiSN Collector — Ops Console",
  description:
    "Read-only operational diagnostics for the LiSN collector — incident verdicts, coverage gaps, and chat over live tool data.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col bg-background text-foreground">
        <a href="#main-content" className="skip-link">
          Skip to main content
        </a>
        <Header />
        <main
          id="main-content"
          className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:px-6 sm:py-8"
        >
          <SourceHealthBanner />
          {children}
        </main>
        <StatusBar />
      </body>
    </html>
  );
}
