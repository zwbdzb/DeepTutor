"use client";

import { useTranslation } from "react-i18next";
import { Plug, UserRound, Wand2, type LucideIcon } from "lucide-react";
import { FeaturePage } from "@/components/layout/FeaturePage";
import SpaceDashboard from "@/components/space/SpaceDashboard";
import { PERSONALIZATION_GROUPS } from "@/components/space/dashboard-groups";
import { PersonalizationShelfIcon } from "@/components/sidebar/ShelfIcons";

/**
 * The hub's heading, word by word: each of the three things this shelf holds
 * wears its own mark, and a single space separates the words. The accessible
 * name is the shelf's own label, so screen readers hear what the sidebar says.
 */
const TITLE_SEGMENTS: ReadonlyArray<{ label: string; icon: LucideIcon }> = [
  { label: "Experts", icon: UserRound },
  { label: "Skills", icon: Wand2 },
  { label: "Connectors", icon: Plug },
];

/** The account's own shelf: the same in every workspace. */
export function PersonalizationHub() {
  const { t } = useTranslation();
  const headingName = t("Experts · Skills · Connectors");
  return (
    <FeaturePage>
      <SpaceDashboard
        icon={PersonalizationShelfIcon}
        title={
          // The marks hug their own word and the words stand apart, so the
          // heading reads as three things rather than one run of text.
          <span
            aria-label={headingName}
            className="inline-flex flex-wrap items-center gap-x-5 gap-y-1"
          >
            {TITLE_SEGMENTS.map(({ label, icon: Icon }) => (
              <span key={label} className="inline-flex items-center gap-1">
                {/* Sized in em so the marks track the page title, which is 27px
                    in the base layout and 30-32px in the others. */}
                <Icon
                  strokeWidth={1.6}
                  aria-hidden
                  className="h-[0.92em] w-[0.92em] shrink-0 -mr-[0.04em] text-[var(--muted-foreground)]"
                />
                {t(label)}
              </span>
            ))}
          </span>
        }
        description={t("navigation.personalizationIntro")}
        groups={PERSONALIZATION_GROUPS}
      />
    </FeaturePage>
  );
}
