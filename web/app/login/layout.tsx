import { LoginGuard } from "@/components/LoginGuard";

export default function LoginLayout({ children }: { children: React.ReactNode }) {
  return <LoginGuard>{children}</LoginGuard>;
}
