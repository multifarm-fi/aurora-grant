import asyncio

from typing import List
from brownie import network, chain
from brownie.convert import to_address
from multifarm_masterchef.helpers import get_tokens_prices, calculate_apy, to_async
from multifarm_masterchef.models import (
    AprInfo,
    Blockchain,
    Exchange,
    Farm,
    FarmUpdate,
    PoolFees,
    PoolLinks,
    SinglePool,
    TokenInfo,
    YieldType,
)
from multifarm_masterchef.aurora.helpers import *
from multifarm_masterchef.aurora.auroraswap.constants import (
    MASTERCHEF_ABI,
    MASTERCHEF_ADDRESS,
    AURORA_TOKENS,
)
from multifarm_masterchef.aurora.enums import TokenTypeEnum
from multifarm_masterchef.aurora.serializers import (
    MasterChefData,
    PoolToken,
    PoolInfo,
)


class Auroraswap:
    FARM_URL = "https://app.auroraswap.net/"
    FARM_ID = Farm.AURORASWAP
    EXCHANGE_NAME = Exchange.AURORASWAP
    EXCHANGE_URL = "https://app.auroraswap.net/"
    BLOCKCHAIN = Blockchain.AURORA
    ACTIVE = True
    REWARD_TOKEN_TICKER = "BRL"
    REWARD_TOKEN_ADDRESS = "0x12c87331f086c3c926248f964f8702c0842fd77f"
    POOLS_LINK = "https://app.auroraswap.net/pools"
    FARMS_LINK = "https://app.auroraswap.net/farms"

    tokens = []
    prices = {}

    def __init__(
        self, masterchef_address=MASTERCHEF_ADDRESS, masterchef_abi=MASTERCHEF_ABI
    ) -> None:
        network.connect("aurora-main")
        brownie.multicall(to_address(masterchef_address))
        self.contract = Contract.from_abi(
            name="masterchef",
            address=masterchef_address,
            abi=masterchef_abi,
        )
        self.prices = asyncio.run(get_tokens_prices(AURORA_TOKENS))

    def get_pools(self) -> List[PoolInfo]:
        masterchef_data = self.get_masterchef_data()
        pools = self.get_pools_info(masterchef_data.pool_count)
        self.tokens = self.get_tokens(pools)

        loop = self._get_or_create_eventloop()
        tasks = [self.get_pool_metrics(pool) for pool in pools]
        pools = loop.run_until_complete(asyncio.gather(*tasks))
        return pools

    def calculate_supply_aprs(
        self, pools: List[PoolInfo], masterchef_data: MasterChefData
    ):
        pools_for_db = []
        for pool_info in pools:
            farm_update = None
            if pool_info.pool_token.type == TokenTypeEnum.UNI:
                farm_update = (
                    self._get_uni_pool_info(pool_info, masterchef_data)
                    if pool_info.metrics
                    else None
                )
            elif pool_info.pool_token.type == TokenTypeEnum.ERC20:
                farm_update = (
                    self._get_single_pool_info(pool_info, masterchef_data)
                    if pool_info.metrics
                    else None
                )
            else:
                pass

            if farm_update:
                pools_for_db.append(farm_update.format_for_db())

        return pools_for_db

    @to_async()
    def get_pool_metrics(self, pool: PoolInfo):
        if pool.pool_token.type == TokenTypeEnum.ERC20:
            metrics = get_erc20_metrics(self.prices, pool.pool_token)
            pool.metrics = metrics
        if pool.pool_token.type == TokenTypeEnum.UNI:
            metrics = get_uni_metrics(self.tokens, self.prices, pool.pool_token)
            pool.metrics = metrics
        return pool

    def get_tokens(self, pools: List[PoolInfo]) -> Dict[str, PoolToken]:
        tokens = {}
        tokens_addresses = []
        for pool in pools:
            if pool.pool_token:
                tokens_addresses.extend(pool.pool_token.tokens)

        tokens_addresses = list(set(tokens_addresses))

        loop = self._get_or_create_eventloop()
        tasks = [
            self.get_token(addr, self.contract.address) for addr in tokens_addresses
        ]
        tokens_infos = loop.run_until_complete(asyncio.gather(*tasks))
        for token_info in tokens_infos:  # type: PoolToken
            tokens.setdefault(token_info.address.lower(), token_info)

        return tokens

    def get_pools_info(self, pool_count: int) -> List[PoolInfo]:
        pool_infos = {}
        loop = self._get_or_create_eventloop()
        tasks = [self.get_pool_info(index) for index in range(pool_count)]
        pools = loop.run_until_complete(asyncio.gather(*tasks))
        for pool in pools:  # type: PoolInfo
            pool_infos.setdefault(pool.address, pool)

        loop = self._get_or_create_eventloop()
        tasks = [
            self.get_token(addr, self.contract.address) for addr in pool_infos.keys()
        ]
        tokens_infos = loop.run_until_complete(asyncio.gather(*tasks))
        for token_info in tokens_infos:  # type: PoolToken
            pool_infos[token_info.address].pool_token = token_info

        return list(pool_infos.values())

    def get_masterchef_data(self) -> MasterChefData:
        current_block = len(chain)
        with brownie.multicall:
            multiplier = self.contract.getMultiplier(current_block, current_block + 1)
            rewards_per_week = (
                self.contract.BRLPerBlock() / 1e18 * multiplier * 604800 / 1.1
            )
            pool_count = self.contract.poolLength()
            total_alloc_points = self.contract.totalAllocPoint()
            reward_token_address = self.contract.BRL()
        return MasterChefData(
            rewards_per_week=rewards_per_week,
            pool_count=pool_count,
            total_alloc_points=total_alloc_points,
            reward_token_address=str(reward_token_address).lower(),
        )

    @to_async()
    def get_pool_info(self, pool_index: int) -> PoolInfo:
        (
            lp_token,
            alloc_points,
            last_reward_block,
            acc_brl_per_share,
            deposit_fee,
        ) = self.contract.poolInfo(pool_index)
        return PoolInfo(
            address=lp_token,
            alloc_points=alloc_points,
            deposit_fee=deposit_fee,
        )

    @staticmethod
    @to_async()
    def get_token(address: str, staking_address: str) -> Union[PoolToken, None]:
        print(f"Getting token info for: {address}")
        try:
            info = get_curve_info(address, staking_address)
            return info
        except ValueError:
            pass
        try:
            info = get_stableswap_info(address, staking_address)
            return info
        except ValueError:
            pass
        try:
            info = get_uni_info(address, staking_address)
            return info
        except ValueError:
            pass
        try:
            info = get_harvest_vault_info(address, staking_address)
            return info
        except ValueError:
            pass
        try:
            info = get_erc20_info(address, staking_address)
            return info
        except ValueError:
            pass

    def _get_uni_pool_info(
        self, pool_info: PoolInfo, masterchef_data: MasterChefData
    ) -> FarmUpdate:
        a_token = pool_info.metrics.token0
        b_token = pool_info.metrics.token1
        pool_rewards_per_week = (
            pool_info.alloc_points
            / masterchef_data.total_alloc_points
            * masterchef_data.rewards_per_week
        )
        reward_price = self.prices[self.REWARD_TOKEN_ADDRESS.lower()]
        usd_per_week = pool_rewards_per_week * reward_price
        weekly_apr = usd_per_week / pool_info.metrics.staked_tvl * 100
        daily_apr = weekly_apr / 7
        yearly_apr = weekly_apr * 52

        token_info = TokenInfo(
            token_a=a_token.symbol,
            token_b=b_token.symbol,
            reward_token_a=self.REWARD_TOKEN_TICKER,
            reward_token_b="",
            token_a_address=a_token.address,
            token_b_address=b_token.address,
        )
        apr_info = AprInfo(
            apr_yearly=yearly_apr,
            apr_weekly=weekly_apr,
            apr_daily=daily_apr,
            apy_yearly=calculate_apy(yearly_apr),
            fee_apr_yearly=yearly_apr,
            reward_token_a_apr_yearly=yearly_apr,
            reward_token_b_apr_yearly=0,
        )
        pool_links = PoolLinks(
            investment_link=self.POOLS_LINK,
            staking_link=self.POOLS_LINK,
        )
        fees = PoolFees(
            deposit_fee=pool_info.deposit_fee,
            withdraw_fee=0,
            harvest_lockup=False,
            harvest_lockup_info="",
            transfer_tax=False,
            transfer_tax_info="",
            anti_whale=0,
        )
        pool = SinglePool(
            asset=pool_info.pool_token.symbol,
            tvl_staked=int(pool_info.metrics.staked_tvl),
            date_added="01/01/2021",
            other_pool_economics_infos="",
            asset_address=pool_info.pool_token.address,
            audit_info="",
            yield_type=YieldType.LP_STAKE,
            token_info=token_info,
            apr_info=apr_info,
            pool_links=pool_links,
            fees=fees,
        )
        farm_update = FarmUpdate(
            active=self.ACTIVE,
            farm_id=self.FARM_ID,
            url=self.FARM_URL,
            blockchain=self.BLOCKCHAIN,
            exchange_name=self.EXCHANGE_NAME,
            exchange_url=self.EXCHANGE_URL,
            pool=pool,
        )

        return farm_update

    def _get_single_pool_info(
        self, pool_info: PoolInfo, masterchef_data: MasterChefData
    ) -> FarmUpdate:
        a_token = pool_info.pool_token
        pool_rewards_per_week = (
            pool_info.alloc_points
            / masterchef_data.total_alloc_points
            * masterchef_data.rewards_per_week
        )
        reward_price = self.prices[self.REWARD_TOKEN_ADDRESS.lower()]
        usd_per_week = pool_rewards_per_week * reward_price
        weekly_apr = (
            usd_per_week / pool_info.metrics.staked_tvl * 100
            if pool_info.metrics
            else None
        )
        daily_apr = weekly_apr / 7 if weekly_apr else None
        yearly_apr = weekly_apr * 52 if weekly_apr else None

        token_info = TokenInfo(
            token_a=a_token.symbol,
            token_b="",
            reward_token_a=self.REWARD_TOKEN_TICKER,
            reward_token_b="",
            token_a_address=a_token.address,
            token_b_address="",
        )
        apr_info = AprInfo(
            apr_yearly=yearly_apr,
            apr_weekly=weekly_apr,
            apr_daily=daily_apr,
            apy_yearly=calculate_apy(yearly_apr) if yearly_apr else None,
            fee_apr_yearly=yearly_apr,
            reward_token_a_apr_yearly=yearly_apr,
            reward_token_b_apr_yearly=0,
        )
        pool_links = PoolLinks(
            investment_link=self.POOLS_LINK,
            staking_link=self.POOLS_LINK,
        )
        fees = PoolFees(
            deposit_fee=pool_info.deposit_fee,
            withdraw_fee=0,
            harvest_lockup=False,
            harvest_lockup_info="",
            transfer_tax=False,
            transfer_tax_info="",
            anti_whale=0,
        )
        pool = SinglePool(
            asset=pool_info.pool_token.symbol,
            tvl_staked=int(pool_info.metrics.staked_tvl) if pool_info.metrics else None,
            date_added="01/01/2021",
            other_pool_economics_infos="",
            asset_address=pool_info.pool_token.address,
            audit_info="",
            yield_type=YieldType.SINGLE_STAKE,
            token_info=token_info,
            apr_info=apr_info,
            pool_links=pool_links,
            fees=fees,
        )
        farm_update = FarmUpdate(
            active=self.ACTIVE,
            farm_id=self.FARM_ID,
            url=self.FARM_URL,
            blockchain=self.BLOCKCHAIN,
            exchange_name=self.EXCHANGE_NAME,
            exchange_url=self.EXCHANGE_URL,
            pool=pool,
        )

        return farm_update

    @staticmethod
    def _get_or_create_eventloop():
        try:
            return asyncio.get_event_loop()
        except RuntimeError as ex:
            if "There is no current event loop in thread" in str(ex):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                return asyncio.get_event_loop()


if __name__ == "__main__":
    from datetime import datetime

    start = datetime.now()
    auroraswap = Auroraswap()
    masterchef_data = auroraswap.get_masterchef_data()
    pools = auroraswap.get_pools()
    print(pools)
    pools = auroraswap.calculate_supply_aprs(pools, masterchef_data)
    print(pools)
    end = datetime.now()
    print(end - start)