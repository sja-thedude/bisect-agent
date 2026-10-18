import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Bisect — autonomous regression hunter",
  description: "Paste a repo and a bug report. Bisect writes a failing test, git-bisects in a sandbox, and explains the culprit commit. Powered by NVIDIA Nemotron on Nebius Token Factory.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
      </head>
      <body className="min-h-screen">{children}</body>
    </html>
  );
}
