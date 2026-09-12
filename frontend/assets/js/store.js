/** Mutable state shared across modules; each renderer reads what it needs from here. */

export const store = {
  regenAt: null,
  packsNow: 0,
  packsMax: 0,
  currentAccountId: null,
  currentAccountName: '',
  accounts: [],
  accountsHtml: '',
  seriesMenu: [],
  seriesKey: '',
  seriesList: [],
  feed: [],
  logFilter: 'all',
  engineStatus: 'idle',
  lastStats: null,
  marketLoaded: false,
};
