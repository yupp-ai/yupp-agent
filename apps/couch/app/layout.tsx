import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Couch",
  description: "Agent platform you can sit on.",
  icons: { icon: "/logo.png" },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
