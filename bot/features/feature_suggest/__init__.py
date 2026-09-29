from .cog import FeatureSuggest

async def setup(bot):
    await bot.add_cog(FeatureSuggest(bot))