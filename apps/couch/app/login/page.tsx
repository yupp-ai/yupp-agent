"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

const ERROR_MESSAGES: Record<string, string> = {
  authentication: "Sign-in failed. Try again.",
  unauthorized:
    "This Google account isn't registered with AHS. Ask an admin to add it, then retry.",
  session_invalidated: "Your session is no longer valid. Sign in again.",
};

function LoginContent() {
  const searchParams = useSearchParams();
  const error = searchParams.get("error");
  const redirectTo = searchParams.get("redirectTo") ?? "/";
  const [loading, setLoading] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  async function handleLogin() {
    setLoading(true);
    setSubmitError(null);
    try {
      const url = `/api/authentication/google/login?redirectTo=${encodeURIComponent(redirectTo)}`;
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) throw new Error(await res.text());
      const data = (await res.json()) as { redirectUrl?: string };
      if (!data.redirectUrl) throw new Error("Missing redirect URL");
      window.location.href = data.redirectUrl;
    } catch {
      setSubmitError("Unable to start Google sign-in.");
      setLoading(false);
    }
  }

  return (
    <div className="login-shell">
      <div className="login-card">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img className="logo" src="/logo.png" alt="Couch" />
        <div className="brand">Couch</div>
        <div className="tag">an agent platform you can sit on.</div>
        <button
          type="button"
          className="google-btn"
          onClick={handleLogin}
          disabled={loading}
        >
          <svg className="g" viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844a4.14 4.14 0 0 1-1.797 2.717v2.258h2.908c1.702-1.567 2.685-3.874 2.685-6.616z" />
            <path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.184l-2.908-2.258c-.806.54-1.836.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.331A8.997 8.997 0 0 0 9 18z" />
            <path fill="#FBBC05" d="M3.964 10.707a5.41 5.41 0 0 1-.282-1.707c0-.593.102-1.17.282-1.707V4.962H.957A8.997 8.997 0 0 0 0 9c0 1.452.348 2.827.957 4.038l3.007-2.331z" />
            <path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.892 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.962L3.964 7.293C4.672 5.166 6.656 3.58 9 3.58z" />
          </svg>
          {loading ? "Preparing…" : "Sign in with Google"}
        </button>
        {(submitError || error) && (
          <p className="login-error">
            {submitError ?? ERROR_MESSAGES[error ?? ""] ?? "Unable to sign in."}
          </p>
        )}
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="login-shell" />}>
      <LoginContent />
    </Suspense>
  );
}
