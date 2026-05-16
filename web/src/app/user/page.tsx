"use client";

import { ImagePageContent } from "@/app/image/image-page-content";

export default function PublicUserImagePage() {
  return <ImagePageContent isAdmin={false} publicMode />;
}
