import asyncio
from typing import Any, cast

import httpx
from typing_extensions import override

from torrra._types import Torrent, TorrentDict
from torrra.core.cache import cache
from torrra.core.exceptions import IndexerError
from torrra.indexers.base import BaseIndexer


class JackettIndexer(BaseIndexer):
    @override
    def get_search_url(self) -> str:
        return f"{self.url}/api/v2.0/indexers/all/results"

    @override
    def get_healthcheck_url(self) -> str:
        return f"{self.url}/api/v2.0/indexers/nonexistent_indexer/results"

    @override
    async def search(self, query: str, use_cache: bool = True) -> list[Torrent] | None:
        key = cache.make_key("jackett", query)

        if use_cache and key in cache:
            raw_data = cast(list[TorrentDict], cache.get(key))
            return [Torrent.from_dict(d) for d in raw_data]

        url = self.get_search_url()
        params = {"apikey": self.api_key, "query": query}

        for i in range(self.max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()

                data = resp.json()
                self._raise_if_all_indexers_failed(data)

                torrents = [self._normalize_result(r) for r in data["Results"]]
                if use_cache and torrents:
                    cache.set(key, [t.to_dict() for t in torrents])

                return torrents
            except httpx.TimeoutException:
                if i < self.max_retries - 1:
                    await asyncio.sleep(0.5 * 2**i)  # exponential backoff
                else:  # raise error on final attempt
                    raise

    def _raise_if_all_indexers_failed(self, data: dict[str, Any]) -> None:
        # jackett answers 200 with empty results when every indexer errored
        indexers = data.get("Indexers") or []
        if data["Results"] or not indexers:
            return

        errors = [i["Error"] for i in indexers if i.get("Error")]
        if len(errors) == len(indexers):
            first_error = errors[0].strip().splitlines()[0].removeprefix(
                "Jackett.Common.IndexerException: "
            )
            raise IndexerError(
                f"all {len(indexers)} jackett indexers failed\n" + first_error
            )

    @override
    async def healthcheck(self) -> bool:
        url = self.get_healthcheck_url()
        params = {"apikey": self.api_key}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                return True

            except httpx.RequestError:
                raise IndexerError(
                    "could not connect to jackett server\n"
                    + "please make sure jackett server is running and the url is correct"
                )

            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code

                if status_code == 401:
                    raise IndexerError(
                        "invalid jackett server api key\n"
                        + "double-check the api key you provided"
                    )
                elif status_code == 500 and "nonexistent_indexer" in e.response.text:
                    return True
                else:
                    raise IndexerError(
                        f"jackett server returned http {status_code}\n"
                        + "unexpected response from jackett server. please verify your setup"
                    )

    @override
    def _normalize_result(self, r: dict[str, Any]) -> Torrent:
        return Torrent(
            title=r.get("Title", "unknown"),
            size=r.get("Size", 0),
            seeders=r.get("Seeders", 0),
            leechers=r.get("Peers", 0),
            source=r.get("Tracker", "unknown"),
            magnet_uri=r.get("MagnetUri") or r["Link"],
        )
