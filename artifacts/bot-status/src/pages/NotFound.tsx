import { Link } from "wouter";
import { AlertCircle } from "lucide-react";

export default function NotFound() {
  return (
    <div className="flex min-h-[100dvh] w-full flex-col items-center justify-center bg-background p-4 text-center">
      <div className="flex h-20 w-20 items-center justify-center rounded-full bg-muted/50 mb-6">
        <AlertCircle className="h-10 w-10 text-muted-foreground" />
      </div>
      <h1 className="text-2xl font-bold text-foreground">Page Not Found</h1>
      <p className="mt-2 text-muted-foreground max-w-sm">
        The page you are looking for doesn't exist or has been moved.
      </p>
    </div>
  );
}
