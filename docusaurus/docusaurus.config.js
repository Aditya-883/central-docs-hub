// @ts-check
import {themes as prismThemes} from 'prism-react-renderer';

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'Central Docs Hub',
  tagline: 'Automated Documentation Portal',
  favicon: 'img/favicon.ico',

  future: {
    v4: true,
  },

  markdown: {
    mermaid: true,
  },
  themes: ['@docusaurus/theme-mermaid'],

  url: 'https://harshh122.github.io',
  baseUrl: '/central-docs-hub/',

  organizationName: 'harshh122',
  projectName: 'central-docs-hub',
  trailingSlash: false,

  onBrokenLinks: 'warn',

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: false, // Disabled so the 3 separate instances below handle routing
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      }),
    ],
  ],

  plugins: [
    [
      '@docusaurus/plugin-content-docs',
      {
        id: 'user',
        path: 'docs/public', // Change to 'docs/user' if compile_mdx.py outputs there
        routeBasePath: 'docs/user',
        editUrl:
          'https://github.com/harshh122/central-docs-hub/tree/main/docusaurus/',
      },
    ],
    [
      '@docusaurus/plugin-content-docs',
      {
        id: 'client',
        path: 'docs/client',
        routeBasePath: 'docs/client',
        editUrl:
          'https://github.com/harshh122/central-docs-hub/tree/main/docusaurus/',
      },
    ],
    [
      '@docusaurus/plugin-content-docs',
      {
        id: 'admin',
        path: 'docs/admin',
        routeBasePath: 'docs/admin',
        editUrl:
          'https://github.com/harshh122/central-docs-hub/tree/main/docusaurus/',
      },
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      image: 'img/docusaurus-social-card.jpg',
      colorMode: {
        respectPrefersColorScheme: true,
      },
      navbar: {
        title: 'Central Docs Hub',
        logo: {
          alt: 'Central Docs Hub Logo',
          src: 'img/logo.svg',
        },
        items: [
          {
            to: '/docs/user',
            label: 'User Docs',
            position: 'left',
          },
          {
            to: '/docs/client',
            label: 'Client Docs',
            position: 'left',
          },
          {
            to: '/docs/admin',
            label: 'Admin Docs',
            position: 'left',
          },
          {
            href: 'https://github.com/harshh122/central-docs-hub',
            label: 'GitHub',
            position: 'right',
          },
        ],
      },
      footer: {
        style: 'dark',
        links: [
          {
            title: 'Portals',
            items: [
              {
                label: 'User Docs',
                to: '/docs/user',
              },
              {
                label: 'Client Docs',
                to: '/docs/client',
              },
              {
                label: 'Admin Docs',
                to: '/docs/admin',
              },
            ],
          },
          {
            title: 'More',
            items: [
              {
                label: 'GitHub',
                href: 'https://github.com/harshh122/central-docs-hub',
              },
            ],
          },
        ],
        copyright: `Copyright © ${new Date().getFullYear()} Central Docs Hub. Built with Docusaurus.`,
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.dracula,
      },
    }),
};

export default config;
