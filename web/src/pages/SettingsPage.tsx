/**
 * SettingsPage — the hub the twelve config pages moved behind.
 *
 * The pages themselves keep their own top-level routes, so every existing
 * deep link, bookmark, and plugin route override still resolves. What changed
 * is the sidebar: config no longer competes with the work surfaces for
 * attention, it sits one click away under two headings borrowed from the way
 * scientific tooling is usually described — what the agent *can do*, and what
 * *this machine* holds.
 */

import { Link } from "react-router-dom";
import {
  BarChart3,
  BookOpen,
  Cpu,
  FileText,
  FolderOpen,
  KeyRound,
  Package,
  Plug,
  Puzzle,
  Radio,
  Settings,
  ShieldCheck,
  Users,
  Webhook,
  Wrench,
} from "lucide-react";
import type { ComponentType } from "react";

import { useI18n } from "@/i18n";

interface SettingsEntry {
  path: string;
  label: string;
  description: string;
  icon: ComponentType<{ className?: string }>;
}

export const CAPABILITIES: SettingsEntry[] = [
  {
    path: "/models",
    label: "Models",
    description: "Providers, model selection, and auxiliary roles.",
    icon: Cpu,
  },
  {
    path: "/skills",
    label: "Skills",
    description: "Durable procedures the agent can load into a session.",
    icon: Package,
  },
  {
    path: "/mcp",
    label: "MCP servers",
    description: "External tool servers and their authorisation.",
    icon: Plug,
  },
  {
    path: "/plugins",
    label: "Plugins",
    description: "Agent and dashboard extensions, including artifact viewers.",
    icon: Puzzle,
  },
  {
    path: "/channels",
    label: "Channels",
    description: "Slack, Discord, Telegram, and WhatsApp delivery.",
    icon: Radio,
  },
  {
    path: "/webhooks",
    label: "Webhooks",
    description: "Inbound hooks that can start or feed a session.",
    icon: Webhook,
  },
  {
    path: "/pairing",
    label: "Pairing",
    description: "Devices and accounts approved to reach this agent.",
    icon: ShieldCheck,
  },
];

export const WORKSPACE: SettingsEntry[] = [
  {
    path: "/env",
    label: "Keys",
    description: "API keys and credentials, stored outside the repo.",
    icon: KeyRound,
  },
  {
    path: "/profiles",
    label: "Profiles",
    description: "Separate agent identities with their own state.",
    icon: Users,
  },
  {
    path: "/files",
    label: "Files",
    description: "The managed file area on this machine.",
    icon: FolderOpen,
  },
  {
    path: "/config",
    label: "Configuration",
    description: "Every setting, including the raw YAML.",
    icon: Settings,
  },
  {
    path: "/system",
    label: "System",
    description: "Health, backups, checkpoints, and maintenance.",
    icon: Wrench,
  },
  {
    path: "/logs",
    label: "Logs",
    description: "Agent, gateway, and error logs.",
    icon: FileText,
  },
  {
    path: "/analytics",
    label: "Usage",
    description: "Token and cost accounting per model.",
    icon: BarChart3,
  },
  {
    path: "/docs",
    label: "Documentation",
    description: "Reference for commands, tools, and the frame model.",
    icon: BookOpen,
  },
];

/** Every route the Settings hub owns — the sidebar uses this to stay lit. */
export const SETTINGS_PATHS: string[] = [...CAPABILITIES, ...WORKSPACE].map(
  (entry) => entry.path,
);

function Group({
  title,
  blurb,
  entries,
}: {
  title: string;
  blurb: string;
  entries: SettingsEntry[];
}) {
  return (
    <section className="flex flex-col gap-2">
      <div className="flex flex-col gap-0.5">
        <h2 className="text-xs text-display text-text-tertiary">{title}</h2>
        <p className="text-xs text-text-secondary">{blurb}</p>
      </div>
      <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {entries.map((entry) => (
          <li key={entry.path}>
            <Link
              to={entry.path}
              className="flex h-full flex-col gap-1 rounded border border-border px-3 py-2.5 hover:bg-card focus-visible:outline-2 focus-visible:outline-ring"
            >
              <span className="flex items-center gap-2 text-sm text-text-primary">
                <entry.icon className="h-4 w-4 shrink-0" aria-hidden />
                {entry.label}
              </span>
              <span className="text-xs text-text-secondary">
                {entry.description}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

export default function SettingsPage() {
  useI18n();
  return (
    <div className="flex flex-col gap-6 p-4">
      <Group
        title="Capabilities"
        blurb="What the agent can reach and what it can do."
        entries={CAPABILITIES}
      />
      <Group
        title="Workspace"
        blurb="What this machine holds and how it runs."
        entries={WORKSPACE}
      />
    </div>
  );
}
