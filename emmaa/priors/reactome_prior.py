import logging
import requests
from random import sample
from functools import lru_cache
from indra.databases.uniprot_client import get_gene_name
from indra.databases.hgnc_client import get_hgnc_id, get_uniprot_id

from emmaa.priors import SearchTerm

logger = logging.getLogger('reactome_prior')


def make_prior_from_genes(gene_list):
    """Returns reactome prior based on a list of genes

    Parameters
    ----------
    gene_list: list of str
        list of HGNC symbols for genes

    Returns
    -------
    res: list of :py:class:`emmaa.priors.SearchTerm`
        list of search terms corresponding to all genes found in any reactome
        pathway containing one of the genes in the input gene list
    """
    all_reactome_ids = set([])
    for gene_name in gene_list:
        hgnc_id = get_hgnc_id(gene_name)
        uniprot_id = get_uniprot_id(hgnc_id)
        if not uniprot_id:
            logger.warning('Could not get Uniprot ID for HGNC symbol'
                           f' {gene_name}')
            continue
        reactome_ids = rx_id_from_up_id(uniprot_id)
        if not reactome_ids:
            logger.warning('Could not get Reactome ID for HGNC symbol'
                           f' {gene_name}')
            continue
    all_reactome_ids.update(reactome_ids)

    all_pathways = set([])
    test_pathways = []
    for reactome_id in reactome_ids:
        if reactome_id.split('-')[1] != 'HSA':
            continue
        pathways = get_pathways_containing_gene(reactome_id)
        if pathways is not None:
            all_pathways.update(pathways)
            test_pathways.extend(pathways)

    all_genes = set([])
    for pathway in sample(all_pathways, len(all_pathways)):
        genes = get_genes_contained_in_pathway(pathway)
        if genes is not None:
            all_genes.update(genes)

    result = []
    for uniprot_id in all_genes:
        hgnc_name = get_gene_name(uniprot_id)
        if hgnc_name is None:
            logger.warning('Could not get HGNC name for UniProt ID'
                           f' {uniprot_id}')
            continue
        hgnc_id = get_hgnc_id(hgnc_name)
        if not hgnc_id:
            logger.warning(f'{hgnc_name} is not a valid HGNC name')
            continue
        term = SearchTerm(type='gene', name=hgnc_name,
                          search_term=f'"{hgnc_name}"',
                          db_refs={'HGNC': hgnc_id,
                                   'UP': uniprot_id})
        result.append(term)
    return result


@lru_cache(10000)
def rx_id_from_up_id(up_id):
    """Get the Reactome Stable ID for a given Uniprot ID."""
    react_search_url = 'http://www.reactome.org/ContentService/search/query'
    params = {'query': up_id, 'cluster': 'true', 'species': 'Homo sapiens'}
    headers = {'Accept': 'application/json'}
    res = requests.get(react_search_url, headers=headers, params=params)
    if not res.status_code == 200:
        return None
    json = res.json()
    results = json.get('results')
    if not results:
        logger.warning(f'No results for {up_id}')
        return None
    stable_ids = []
    for result in results:
        entries = result.get('entries')
        for entry in entries:
            stable_id = entry.get('stId')
            if not stable_id:
                continue
            stable_ids.append(stable_id)
    return stable_ids


@lru_cache(100000)
def up_id_from_rx_id(reactome_id):
    """Get the Uniprot ID (referenceEntity) for a given Reactome Stable ID."""
    react_url = 'http://www.reactome.org/ContentService/data/query/' \
                + reactome_id + '/referenceEntity'
    res = requests.get(react_url)
    if not res.status_code == 200:
        return None
    _, entry, entry_type = res.text.split('\t')
    if entry_type != 'ReferenceGeneProduct':
        return None
    id_entry = entry.split(' ')[0]
    db_ns, db_id = id_entry.split(':')
    if db_ns != 'UniProt':
        return None
    return db_id


@lru_cache(1000)
def get_pathways_containing_gene(reactome_id):
    """"Get all ids for reactom pathways containing some form of an entity

    Parameters
    ----------
    reactome_id: str
        reactome id for a gene

    Returns
    -------
    pathway_ids: list of str
        list of reactome ids for pathways containing the input gene
    """
    react_url = ('http://www.reactome.org/ContentService/data/pathways/low'
                 f'/entity/{reactome_id}/allForms')
    params = {'species': 'Homo sapiens'}
    headers = {'Accept': 'application/json'}
    res = requests.get(react_url, headers=headers, params=params)
    if not res.status_code == 200:
        logger.warning(f'Request failed for reactome_id {reactome_id}')
        return None
    results = res.json()
    if not results:
        logger.info(f'No results for {reactome_id}')
        return None
    pathway_ids = [pathway['stIdVersion'] for pathway in results]
    return pathway_ids


@lru_cache(1000)
def get_genes_contained_in_pathway(reactome_id):
    """Get all genes contained in a given pathway

    Parameters
    ----------
    reactome_id: strig
        reactome id for a pathway

    Returns
    -------
    genes: list of str
        list of uniprot ids for all unique genes contained in input pathway
    """
    react_url = ('http://www.reactome.org/ContentService/data'
                 f'/participants/{reactome_id}')
    params = {'species': 'Homo species'}
    headers = {'Accept': 'application/json'}
    res = requests.get(react_url, headers=headers, params=params)
    results = res.json()
    if not res.status_code == 200:
        return None
    if not results:
        logger.info(f'No results for {reactome_id}')
    genes = [entity['identifier'] for result in results
             for entity in result['refEntities']
             if entity.get('schemaClass') == 'ReferenceGeneProduct']
    return list(set(genes))


if __name__ == '__main__':
    example_genes = ['KRAS', 'TP53', 'SMAD4', 'TTN', 'CDKN2A']
    prior = make_prior_from_genes(example_genes)
    print(len(prior))
